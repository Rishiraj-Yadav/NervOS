import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { MODEL_PROVIDERS } from "../api/providers";
import {
  agentInstanceQuery,
  agentRunsQuery,
  useCancelRun,
  useCreateRun,
  useUpdateAgentInstance,
} from "../api/queries";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";
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
  // The enable/disable toggle and the configuration form are separate mutations so a failure is
  // reported next to the control that caused it rather than attributed to the other one.
  const updateConfiguration = useUpdateAgentInstance(agentInstanceId);
  const updateEnabled = useUpdateAgentInstance(agentInstanceId);
  const createRun = useCreateRun(agentInstanceId);
  // Cancellation is its own mutation so a failure is reported next to the Run it belongs to,
  // and so a second click while one request is in flight cannot start another.
  const cancelRun = useCancelRun(agentInstanceId);

  async function handleConfigure(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      // A configuration update always sends all three values together; `enabled` is sent by a
      // separate request so one submission maps to exactly one stored change.
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
            <p className="eyebrow">
              {instance.data.agent_key} v{instance.data.agent_definition_version}
            </p>
            <h1 id="agent-title">{instance.data.display_name}</h1>
            <p className="lede">
              One submission creates one independent run. Earlier runs are shown below for your
              reference and are never sent to the model.
            </p>

            <div className="agent-toolbar">
              <button
                type="button"
                className="secondary"
                onClick={() => void toggleEnabled(!instance.data.enabled)}
                disabled={updateEnabled.isPending}
              >
                {instance.data.enabled ? "Disable agent" : "Enable agent"}
              </button>
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
          </>
        ) : null}
      </section>
    </main>
  );
}
