import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { MODEL_PROVIDERS } from "../api/providers";
import {
  packagesQuery,
  usePatchInstanceConfig,
  useRebindInstance,
} from "../api/packageQueries";
import {
  agentInstanceQuery,
  agentRunsQuery,
  useCancelRun,
  useCreateRun,
  useUpdateAgentInstance,
} from "../api/queries";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";
import { ConfigSchemaForm } from "../components/ConfigSchemaForm";
import { RunItem } from "../components/RunItem";
import { RunTimeline } from "../components/RunTimeline";
import { isTerminalStatus } from "../api/agentInstances";
import { NotFoundPage } from "./NotFoundPage";

export function AgentInstancePage() {
  const { agentInstanceId } = useParams<{ agentInstanceId: string }>();
  const instanceId = Number(agentInstanceId);

  if (!Number.isInteger(instanceId) || instanceId <= 0) {
    return <NotFoundPage />;
  }
  return <AgentInstanceView agentInstanceId={instanceId} />;
}

function AgentInstanceView({ agentInstanceId }: { agentInstanceId: number }) {
  const instance = useQuery(agentInstanceQuery(agentInstanceId));
  const runs = useQuery(agentRunsQuery(agentInstanceId));
  const updateConfiguration = useUpdateAgentInstance(agentInstanceId);
  const updateEnabled = useUpdateAgentInstance(agentInstanceId);
  const createRun = useCreateRun(agentInstanceId);
  const cancelRun = useCancelRun(agentInstanceId);

  const activePackages = useQuery(packagesQuery("active"));
  const patchConfigMutation = usePatchInstanceConfig();
  const rebindMutation = useRebindInstance();

  const [showConfigModal, setShowConfigModal] = useState(false);
  const [showRebindModal, setShowRebindModal] = useState(false);
  const [editedConfig, setEditedConfig] = useState<Record<string, unknown>>({});
  const [targetVersion, setTargetVersion] = useState<string>("");
  const [rebindConfig, setRebindConfig] = useState<Record<string, unknown> | null>(null);

  const isPackage = instance.data && !instance.data.agent_key.startsWith("nervos.");
  const matchingVersions = activePackages.data?.items.filter(
    (p) => p.package_id === instance.data?.agent_key
  ) || [];

  async function handlePatchConfig() {
    try {
      await patchConfigMutation.mutateAsync({
        agentInstanceId,
        config: editedConfig,
        expectedConfigRevision: 1, // Default or incremented
      });
      setShowConfigModal(false);
    } catch {
      // Handled in error display
    }
  }

  async function handleRebind() {
    if (!targetVersion) return;
    try {
      await rebindMutation.mutateAsync({
        agentInstanceId,
        targetPackageVersion: targetVersion,
        config: rebindConfig,
        expectedConfigRevision: 1,
      });
      setShowRebindModal(false);
    } catch {
      // Handled in error display
    }
  }

  async function handleConfigure(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await updateConfiguration.mutateAsync({
        display_name: String(data.get("display_name") ?? ""),
        model_provider: String(data.get("model_provider") ?? ""),
        model_name: String(data.get("model_name") ?? ""),
      });
    } catch {
      // Rendered from the mutation's error state below.
    }
  }

  async function handleExecute(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await createRun.mutateAsync(String(data.get("input") ?? ""));
      form.reset();
    } catch {
      // Rendered from the mutation's error state below.
    }
  }

  async function toggleEnabled(enabled: boolean) {
    try {
      await updateEnabled.mutateAsync({ enabled });
    } catch {
      // Rendered from the mutation's error state below.
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
            All agents
          </Link>
        </div>
      </header>

      <section className="dashboard-content" aria-labelledby="agent-title">
        {instance.isPending ? <LoadingState message="Loading agent…" /> : null}

        {instance.isError ? (
          <ErrorState error={instance.error} onRetry={() => void instance.refetch()} />
        ) : null}

        {instance.data !== undefined ? (
          <>
            <div className="flex items-center space-x-2 mb-1">
              <span className="eyebrow" style={{ margin: 0 }}>
                {instance.data.agent_key} v{instance.data.agent_definition_version}
              </span>
              {isPackage && (
                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">
                  Package Agent
                </span>
              )}
            </div>
            <h1 id="agent-title">{instance.data.display_name}</h1>
            <p className="lede">
              One submission creates one independent run. Earlier runs are shown below for your
              reference and are never sent to the model.
            </p>

            <div className="agent-toolbar flex items-center space-x-3">
              <button
                type="button"
                className="secondary"
                onClick={() => void toggleEnabled(!instance.data.enabled)}
                disabled={updateEnabled.isPending}
              >
                {instance.data.enabled ? "Disable agent" : "Enable agent"}
              </button>
              {isPackage && (
                <>
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => setShowConfigModal(true)}
                  >
                    Edit Package Config
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => {
                      setTargetVersion(instance.data.agent_definition_version);
                      setShowRebindModal(true);
                    }}
                  >
                    Rebind / Rollback Version
                  </button>
                </>
              )}
              <span className={instance.data.enabled ? "instance-state" : "instance-state disabled"}>
                {instance.data.enabled ? "Enabled" : "Disabled"}
              </span>
            </div>
            {updateEnabled.error ? <InlineError error={updateEnabled.error} /> : null}

            <section className="panel run-panel" aria-labelledby="prompt-title">
              <h2 id="prompt-title">Run this agent</h2>
              <form onSubmit={(event) => void handleExecute(event)}>
                <label htmlFor="input">Message</label>
                <textarea
                  id="input"
                  name="input"
                  rows={3}
                  required
                  maxLength={4000}
                  disabled={!instance.data.enabled || createRun.isPending}
                />
                <button
                  type="submit"
                  disabled={!instance.data.enabled || createRun.isPending}
                >
                  {createRun.isPending ? "Submitting…" : "Run agent"}
                </button>
              </form>
              {!instance.data.enabled ? (
                <p className="field-hint" role="note">
                  This agent is disabled, so it cannot start new runs. Existing runs remain.
                </p>
              ) : null}
              {createRun.isPending ? (
                <p className="run-pending" role="status">
                  Submitting the run…
                </p>
              ) : null}
              {createRun.error ? <InlineError error={createRun.error} /> : null}
            </section>

            <section className="panel config-panel" aria-labelledby="config-title">
              <h2 id="config-title">Configuration</h2>
              <form onSubmit={(event) => void handleConfigure(event)}>
                <label htmlFor="display_name">Display name</label>
                <input
                  id="display_name"
                  name="display_name"
                  type="text"
                  required
                  maxLength={100}
                  defaultValue={instance.data.display_name}
                  key={`name-${instance.data.updated_at}`}
                />

                <label htmlFor="model_provider">Model provider</label>
                <select
                  id="model_provider"
                  name="model_provider"
                  defaultValue={instance.data.model_provider}
                  key={`provider-${instance.data.updated_at}`}
                >
                  {MODEL_PROVIDERS.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.label}
                    </option>
                  ))}
                </select>

                <label htmlFor="model_name">Model</label>
                <input
                  id="model_name"
                  name="model_name"
                  type="text"
                  required
                  maxLength={256}
                  defaultValue={instance.data.model_name}
                  key={`model-${instance.data.updated_at}`}
                />
                <p className="field-hint">
                  Changing the model affects only future runs. Existing runs keep their snapshot.
                </p>

                <button type="submit" disabled={updateConfiguration.isPending}>
                  {updateConfiguration.isPending ? "Saving…" : "Save configuration"}
                </button>
              </form>
              {updateConfiguration.error ? <InlineError error={updateConfiguration.error} /> : null}
            </section>

            <section aria-labelledby="history-title">
              <h2 id="history-title">Run history</h2>
              <p className="field-hint">
                Each run below was created by a separate submission. Nothing here is replayed into a
                later request.
              </p>

              {runs.isPending ? <LoadingState message="Loading runs…" /> : null}
              {runs.isError ? (
                <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
              ) : null}
              {runs.data !== undefined && runs.data.items.length === 0 ? (
                <p className="empty-note">No runs yet.</p>
              ) : null}
              {runs.data !== undefined && runs.data.items.length > 0 ? (
                <>
                  <div className="run-list">
                    {runs.data.items.map((run) => (
                      <RunItem
                        key={run.id}
                        run={run}
                        onCancel={(runId) => void cancelRun.mutate(runId)}
                        isCancelling={
                          cancelRun.isPending && cancelRun.variables === run.id
                        }
                        timeline={
                          <RunTimeline runId={run.id} isTerminal={isTerminalStatus(run.status)} />
                        }
                      />
                    ))}
                  </div>
                  {runs.data.next_before_id !== null ? (
                    <p className="field-hint" role="note">
                      Only the most recent runs are listed in this milestone; older runs are not
                      shown.
                    </p>
                  ) : null}
                </>
              ) : null}
            </section>

            {showConfigModal && (
              <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
                <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-lg w-full space-y-4 shadow-xl border dark:border-gray-700">
                  <h3 className="text-lg font-bold text-gray-900 dark:text-gray-100">
                    Edit Package Configuration
                  </h3>
                  <p className="text-xs text-gray-500">
                    Configuration updates affect future runs only. Immutable fields cannot be modified.
                  </p>

                  <ConfigSchemaForm
                    schema={{}}
                    initialConfig={editedConfig}
                    onChange={setEditedConfig}
                  />

                  {patchConfigMutation.error && <InlineError error={patchConfigMutation.error} />}

                  <div className="flex justify-end space-x-3 pt-2">
                    <button
                      type="button"
                      onClick={() => setShowConfigModal(false)}
                      className="button secondary"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => void handlePatchConfig()}
                      disabled={patchConfigMutation.isPending}
                      className="button primary"
                    >
                      {patchConfigMutation.isPending ? "Saving…" : "Save Configuration"}
                    </button>
                  </div>
                </div>
              </div>
            )}

            {showRebindModal && (
              <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
                <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-lg w-full space-y-4 shadow-xl border dark:border-gray-700">
                  <h3 className="text-lg font-bold text-gray-900 dark:text-gray-100">
                    Rebind / Rollback Agent Version
                  </h3>
                  <p className="text-xs text-gray-500">
                    Rebinding switches the definition version for future runs. Historical accepted runs remain pinned to their original version.
                  </p>

                  <div className="space-y-2">
                    <label htmlFor="target-version-select" className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                      Target Version
                    </label>
                    <select
                      id="target-version-select"
                      value={targetVersion}
                      onChange={(e) => setTargetVersion(e.target.value)}
                      className="w-full p-2 border rounded dark:bg-gray-800 dark:border-gray-700 text-sm"
                    >
                      {matchingVersions.map((p) => (
                        <option key={p.package_version} value={p.package_version}>
                          {p.display_name} (v{p.package_version}) {p.package_version === instance.data.agent_definition_version ? "- Current" : ""}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="pt-2 border-t dark:border-gray-700">
                    <span className="text-xs text-gray-500 block mb-1">
                      Target Configuration (optional - leave empty to attempt automatic carry-forward)
                    </span>
                    <ConfigSchemaForm
                      schema={{}}
                      initialConfig={rebindConfig || {}}
                      onChange={setRebindConfig}
                    />
                  </div>

                  {rebindMutation.error && <InlineError error={rebindMutation.error} />}

                  <div className="flex justify-end space-x-3 pt-2">
                    <button
                      type="button"
                      onClick={() => setShowRebindModal(false)}
                      className="button secondary"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleRebind()}
                      disabled={rebindMutation.isPending || targetVersion === instance.data.agent_definition_version}
                      className="button primary"
                    >
                      {rebindMutation.isPending ? "Rebinding…" : "Confirm Rebind"}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </>
        ) : null}
      </section>
    </main>
  );
}
