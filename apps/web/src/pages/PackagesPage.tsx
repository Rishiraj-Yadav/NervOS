import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { packagesQuery } from "../api/packageQueries";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";

export function PackagesPage() {
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const packages = useQuery(packagesQuery(statusFilter));

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <Brand />
        <div className="account-actions">
          <Link className="button-link secondary" to="/dashboard">
            Dashboard
          </Link>
          <Link className="button-link secondary" to="/agents">
            Agents
          </Link>
        </div>
      </header>

      <section className="dashboard-content" aria-labelledby="packages-title">
        <div className="flex justify-between items-center mb-4">
          <div>
            <p className="eyebrow">Installed software</p>
            <h1 id="packages-title">Packages</h1>
            <p className="lede">
              Node-global agent packages installed on this self-hosted NervOS instance.
            </p>
          </div>
          <Link className="button-link primary" to="/packages/install">
            Install Package
          </Link>
        </div>

        <div className="flex space-x-2 mb-4">
          <button
            type="button"
            className={`px-3 py-1 text-sm rounded ${statusFilter === undefined ? "bg-blue-600 text-white" : "bg-gray-200 dark:bg-gray-700"}`}
            onClick={() => setStatusFilter(undefined)}
          >
            All
          </button>
          <button
            type="button"
            className={`px-3 py-1 text-sm rounded ${statusFilter === "active" ? "bg-blue-600 text-white" : "bg-gray-200 dark:bg-gray-700"}`}
            onClick={() => setStatusFilter("active")}
          >
            Active
          </button>
          <button
            type="button"
            className={`px-3 py-1 text-sm rounded ${statusFilter === "failed" ? "bg-blue-600 text-white" : "bg-gray-200 dark:bg-gray-700"}`}
            onClick={() => setStatusFilter("failed")}
          >
            Failed
          </button>
          <button
            type="button"
            className={`px-3 py-1 text-sm rounded ${statusFilter === "pending_removal" ? "bg-blue-600 text-white" : "bg-gray-200 dark:bg-gray-700"}`}
            onClick={() => setStatusFilter("pending_removal")}
          >
            Pending Removal
          </button>
        </div>

        {packages.isPending ? <LoadingState message="Loading installed packages…" /> : null}

        {packages.isError ? (
          <ErrorState error={packages.error} onRetry={() => void packages.refetch()} />
        ) : null}

        {packages.data !== undefined && packages.data.items.length === 0 ? (
          <div className="panel empty-card">
            <h2>No packages installed</h2>
            <p>Upload and install a <code>.nervos</code> package artifact to get started.</p>
            <Link className="button-link" to="/packages/install" style={{ marginTop: "1rem" }}>
              Install package
            </Link>
          </div>
        ) : null}

        {packages.data !== undefined && packages.data.items.length > 0 ? (
          <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
            <table className="w-full text-left border-collapse" aria-label="Installed Packages Table">
              <thead>
                <tr className="border-b dark:border-gray-700 bg-gray-50 dark:bg-gray-800 text-xs font-semibold uppercase text-gray-500">
                  <th className="p-3">Package / Version</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Bound Agents</th>
                  <th className="p-3">Signer Fingerprint</th>
                  <th className="p-3">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y dark:divide-gray-700 text-sm">
                {packages.data.items.map((pkg) => (
                  <tr key={`${pkg.package_id}@${pkg.package_version}`} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                    <td className="p-3">
                      <div className="font-semibold text-gray-900 dark:text-gray-100">{pkg.display_name}</div>
                      <div className="text-xs font-mono text-gray-500">{pkg.package_id}@{pkg.package_version}</div>
                    </td>
                    <td className="p-3">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                        pkg.status === "active" ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" :
                        pkg.status === "failed" ? "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200" :
                        pkg.status === "pending_removal" ? "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200" :
                        "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300"
                      }`}>
                        {pkg.status}
                      </span>
                    </td>
                    <td className="p-3">{pkg.bound_instances_count}</td>
                    <td className="p-3 font-mono text-xs text-gray-500">
                      {pkg.signer_fingerprint.slice(0, 16)}…
                    </td>
                    <td className="p-3">
                      <Link
                        className="text-blue-600 dark:text-blue-400 hover:underline text-sm font-medium"
                        to={`/packages/${encodeURIComponent(pkg.package_id)}/${encodeURIComponent(pkg.package_version)}`}
                      >
                        Details
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </main>
  );
}
