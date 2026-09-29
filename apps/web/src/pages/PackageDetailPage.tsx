import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import {
  packageDetailQuery,
  packageRemovalPlanQuery,
  useUninstallPackage,
} from "../api/packageQueries";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";
import { NotFoundPage } from "./NotFoundPage";

export function PackageDetailPage() {
  const { packageId, packageVersion } = useParams<{ packageId: string; packageVersion: string }>();
  const [showRemovalModal, setShowRemovalModal] = useState(false);
  const [isCopied, setIsCopied] = useState(false);

  const navigate = useNavigate();
  const pkgId = packageId ? decodeURIComponent(packageId) : "";
  const pkgVer = packageVersion ? decodeURIComponent(packageVersion) : "";

  const detail = useQuery(packageDetailQuery(pkgId, pkgVer));
  const removalPlan = useQuery({
    ...packageRemovalPlanQuery(pkgId, pkgVer),
    enabled: showRemovalModal,
  });
  const uninstallMutation = useUninstallPackage();

  if (!pkgId || !pkgVer) {
    return <NotFoundPage />;
  }

  function handleCopyFingerprint() {
    if (!detail.data) return;
    void navigator.clipboard.writeText(detail.data.signer_fingerprint);
    setIsCopied(true);
    setTimeout(() => setIsCopied(false), 2000);
  }

  async function handleConfirmUninstall() {
    try {
      await uninstallMutation.mutateAsync({ packageId: pkgId, version: pkgVer });
      setShowRemovalModal(false);
      navigate("/packages");
    } catch {
      // Handled in mutation error display
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <Brand />
        <div className="account-actions">
          <Link className="button-link secondary" to="/packages">
            Packages
          </Link>
          <Link className="button-link secondary" to="/agents">
            Agents
          </Link>
        </div>
      </header>

      <section className="dashboard-content" aria-labelledby="package-detail-title">
        {detail.isPending ? <LoadingState message="Loading package details…" /> : null}
        {detail.isError ? (
          <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
        ) : null}

        {detail.data && (
          <div className="space-y-6">
            <div className="flex justify-between items-start">
              <div>
                <p className="eyebrow">Installed package</p>
                <h1 id="package-detail-title">{detail.data.display_name}</h1>
                <p className="font-mono text-sm text-gray-500">{detail.data.package_id}@{detail.data.package_version}</p>
              </div>
              <div className="flex items-center space-x-3">
                <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                  detail.data.status === "active" ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" :
                  detail.data.status === "failed" ? "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200" :
                  detail.data.status === "pending_removal" ? "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200" :
                  "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300"
                }`}>
                  {detail.data.status}
                </span>
                {detail.data.status !== "removed" && (
                  <button
                    type="button"
                    onClick={() => setShowRemovalModal(true)}
                    className="button secondary text-red-600 dark:text-red-400 border-red-300 dark:border-red-800"
                  >
                    Uninstall
                  </button>
                )}
              </div>
            </div>

            <div className="panel space-y-4">
              <h2 className="text-base font-bold text-gray-900 dark:text-gray-100">Package Verification & Metadata</h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">Signer Fingerprint:</span>
                  <div className="flex items-center space-x-2 mt-1">
                    <span className="font-mono text-xs p-1 bg-gray-100 dark:bg-gray-800 rounded break-all select-all">
                      {detail.data.signer_fingerprint}
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
                    {detail.data.content_digest}
                  </p>
                </div>

                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">Entrypoint:</span>
                  <p className="font-mono text-xs mt-1">
                    {detail.data.entrypoint_module}:{detail.data.entrypoint_object}
                  </p>
                </div>

                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">Bound Agent Instances:</span>
                  <p className="text-xs mt-1 font-medium">{detail.data.bound_instances_count}</p>
                </div>
              </div>
            </div>

            {Object.keys(detail.data.config_schema).length > 0 && (
              <div className="panel space-y-3">
                <h2 className="text-base font-bold text-gray-900 dark:text-gray-100">Configuration Schema</h2>
                <pre className="p-3 bg-gray-50 dark:bg-gray-900 border rounded text-xs font-mono overflow-auto max-h-60">
                  {JSON.stringify(detail.data.config_schema, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}

        {showRemovalModal && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
            <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-lg w-full space-y-4 shadow-xl border dark:border-gray-700">
              <h3 className="text-lg font-bold text-gray-900 dark:text-gray-100">
                Uninstall Package: {pkgId}@{pkgVer}
              </h3>

              {removalPlan.isPending && <p className="text-sm text-gray-500">Evaluating removal obligations…</p>}
              {removalPlan.isError && <InlineError error={removalPlan.error} />}

              {removalPlan.data && (
                <div className="space-y-3 text-sm">
                  {removalPlan.data.blocking_reasons.length > 0 && (
                    <div className="p-3 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 rounded text-red-800 dark:text-red-200 text-xs space-y-1">
                      <strong>Removal is currently blocked:</strong>
                      <ul className="list-disc list-inside">
                        {removalPlan.data.blocking_reasons.map((r, i) => (
                          <li key={i}>{r}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {removalPlan.data.can_remove_immediately && (
                    <p className="text-gray-600 dark:text-gray-400">
                      No agent instances or active runs are bound to this package version. Uninstalling will delete its payload files.
                    </p>
                  )}

                  {!removalPlan.data.can_remove_immediately && removalPlan.data.can_begin_removal && (
                    <p className="text-amber-600 dark:text-amber-400">
                      There are {removalPlan.data.nonterminal_runs_count} active run(s). Uninstalling will mark this package <code>pending_removal</code> until runs complete.
                    </p>
                  )}
                </div>
              )}

              {uninstallMutation.error && <InlineError error={uninstallMutation.error} />}

              <div className="flex justify-end space-x-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowRemovalModal(false)}
                  className="button secondary"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void handleConfirmUninstall()}
                  disabled={!removalPlan.data?.can_begin_removal || uninstallMutation.isPending}
                  className="button primary bg-red-600 hover:bg-red-700 text-white"
                >
                  {uninstallMutation.isPending ? "Uninstalling…" : "Confirm Uninstall"}
                </button>
              </div>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
