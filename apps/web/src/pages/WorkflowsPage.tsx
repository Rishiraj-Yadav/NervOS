import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  useWorkflowActions,
  workflowDetailQuery,
  workflowsListQuery,
} from "../api/workflowQueries";
import { agentInstancesQuery } from "../api/queries";
import { ErrorState, LoadingState } from "../components/AsyncState";
import type { WorkflowStatus, WorkflowSummary } from "../api/workflows";
import { WorkflowCheckpoint } from "../components/WorkflowCheckpoint";

const STATUS_LABEL: Record<WorkflowStatus, string> = {
  pending: "Pending",
  runnable: "Runnable",
  running: "Running",
  waiting: "Waiting",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  needs_review: "Needs review",
};

// A workflow that is waiting says *what it is waiting for*. Showing a bare "Waiting" would
// leave an owner unable to tell an intentional long sleep from something that stalled.
function waitDescription(workflow: WorkflowSummary): string | null {
  if (workflow.status !== "waiting") return null;
  switch (workflow.wait_kind) {
    case "time":
      return `Sleeps until ${formatTime(workflow.wakeup_at)}`;
    case "signal":
      return `Waiting for the "${workflow.signal_key ?? "unknown"}" signal`;
    case "owner_decision":
      return `Waiting for your decision on "${workflow.decision_key ?? "a proposed action"}"`;
    default:
      return "Waiting";
  }
}

function formatTime(value: string | null): string {
  if (value === null) return "an unknown time";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "an unknown time" : parsed.toLocaleString();
}

function budgetUsed(used: number, allowed: number): string {
  return allowed > 0 ? `${used} / ${allowed}` : `${used}`;
}

export function WorkflowsPage() {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const listQ = useQuery(workflowsListQuery());
  const detailQ = useQuery(workflowDetailQuery(selectedId));
  const agentsQ = useQuery(agentInstancesQuery());
  const actions = useWorkflowActions();
  const navigate = useNavigate();

  const [newOpen, setNewOpen] = useState(false);
  const [agentId, setAgentId] = useState<number | "">("");
  const [inputText, setInputText] = useState("");
  const [submissionKey, setSubmissionKey] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [workflowKind, setWorkflowKind] = useState("research");
  const [signalPayload, setSignalPayload] = useState("{}");
  const [signalError, setSignalError] = useState<string | null>(null);

  if (listQ.isPending || agentsQ.isPending) return <LoadingState />;
  if (listQ.isError)
    return <ErrorState error={listQ.error} onRetry={() => void listQ.refetch()} />;
  if (agentsQ.isError)
    return <ErrorState error={agentsQ.error} onRetry={() => void agentsQ.refetch()} />;

  const workflows = listQ.data.workflows;
  const detail = selectedId !== null ? detailQ.data : undefined;
  const busy = actions.setPaused.isPending || actions.cancel.isPending;

  function submitNew(event: React.FormEvent) {
    event.preventDefault();
    setCreateError(null);
    if (agentId === "" || inputText.trim() === "" || submissionKey.trim() === "") {
      setCreateError("Choose an agent and provide both a request and a submission key.");
      return;
    }
    actions.create.mutate(
      {
        agent_instance_id: Number(agentId),
        submission_key: submissionKey.trim(),
        input_text: inputText.trim(),
        workflow_kind: workflowKind.trim(),
      },
      {
        onSuccess: (created) => {
          setNewOpen(false);
          setInputText("");
          setSubmissionKey("");
          setSelectedId(created.id);
        },
        onError: (error) =>
          setCreateError(error instanceof Error ? error.message : "Could not start the workflow."),
      },
    );
  }

  return (
    <main className="page-shell workflows-page">
      <header className="page-header">
        <h1>Workflows</h1>
        <button type="button" onClick={() => setNewOpen((open) => !open)}>
          {newOpen ? "Cancel" : "New workflow"}
        </button>
      </header>

      {newOpen && (
        <section className="panel" aria-label="Start a workflow">
          <form onSubmit={submitNew}>
            <label htmlFor="workflow-agent">Agent</label>
            <select
              id="workflow-agent"
              value={agentId}
              onChange={(e) => setAgentId(e.target.value === "" ? "" : Number(e.target.value))}
            >
              <option value="">Select an agent</option>
              {agentsQ.data.items.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.display_name}
                </option>
              ))}
            </select>

            <label htmlFor="workflow-kind">Workflow kind</label>
            <input id="workflow-kind" value={workflowKind} required pattern="[a-z0-9][a-z0-9_-]{0,63}"
              onChange={(event) => setWorkflowKind(event.target.value)} />
            <p className="hint">Choose the kind supported by your package, such as research or mail-triage.</p>

            <label htmlFor="workflow-request">Request</label>
            <textarea
              id="workflow-request"
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
            />

            <label htmlFor="workflow-key">Submission key</label>
            <input
              id="workflow-key"
              value={submissionKey}
              onChange={(e) => setSubmissionKey(e.target.value)}
              placeholder="research-2026-10-04-001"
            />
            <p className="hint">
              Reusing a key with the same request returns the original workflow instead of
              starting a second one.
            </p>

            {createError && (
              <p className="form-error text-danger" role="alert">
                {createError}
              </p>
            )}
            <button type="submit" disabled={actions.create.isPending}>
              Start
            </button>
          </form>
        </section>
      )}

      {[actions.setPaused.error, actions.cancel.error, actions.decide.error, actions.signal.error]
        .filter(Boolean).map((error, index) => (
          <p role="alert" key={index}>{error instanceof Error ? error.message : "Action failed."}</p>
        ))}

      {workflows.length === 0 ? (
        <section className="panel" aria-label="No workflows">
          <p>No workflows yet. A workflow keeps its progress while it is idle.</p>
        </section>
      ) : (
        <section className="panel" aria-label="Workflows">
          <ul>
            {workflows.map((workflow) => {
              const waiting = waitDescription(workflow);
              const needsAttention = workflow.status === "needs_review";
              return (
                <li key={workflow.id}>
                  <button type="button" onClick={() => setSelectedId(workflow.id)}>
                    #{workflow.id} {workflow.workflow_kind}
                  </button>
                  <span role="status">{STATUS_LABEL[workflow.status]}</span>
                  {workflow.paused && <span>Paused</span>}
                  {waiting && <span>{waiting}</span>}
                  {needsAttention && workflow.review_reason && (
                    <span>Reason: {workflow.review_reason}</span>
                  )}
                  <span>
                    Steps {budgetUsed(workflow.budget.steps_used, workflow.budget.steps_allowed)}
                  </span>
                  <span>Deadline {formatTime(workflow.deadline_at)}</span>
                  <button
                    type="button"
                    disabled={busy || ["succeeded", "failed", "cancelled", "needs_review"].includes(workflow.status)}
                    onClick={() =>
                      actions.setPaused.mutate({ id: workflow.id, paused: !workflow.paused })
                    }
                  >
                    {workflow.paused ? "Resume" : "Pause"}
                  </button>
                  <button
                    type="button"
                    disabled={busy || ["succeeded", "failed", "cancelled", "needs_review"].includes(workflow.status)}
                    onClick={() => actions.cancel.mutate(workflow.id)}
                  >
                    Cancel
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {selectedId !== null && (
        <section className="panel" aria-label="Workflow detail">
          {detailQ.isPending && <LoadingState message="Loading workflow…" />}
          {detailQ.isError && (
            <ErrorState error={detailQ.error} onRetry={() => void detailQ.refetch()} />
          )}
          {detail && (
            <>
              <h2>Workflow #{detail.workflow.id}</h2>
              <p role="status">{STATUS_LABEL[detail.workflow.status]}</p>
              {detail.workflow.status === "waiting" && detail.workflow.wait_kind === "signal" && (
                <form aria-label="Deliver workflow signal" onSubmit={(event) => {
                  event.preventDefault();
                  setSignalError(null);
                  try {
                    const payload: unknown = JSON.parse(signalPayload);
                    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
                      throw new Error("Signal payload must be a JSON object.");
                    }
                    actions.signal.mutate({id: detail.workflow.id,
                      signalKey: detail.workflow.signal_key ?? "",
                      payload: payload as Record<string, unknown>,
                      expectedRevision: detail.workflow.checkpoint_revision});
                  } catch (error) { setSignalError(error instanceof Error ? error.message : "Invalid JSON."); }
                }}>
                  <label htmlFor="workflow-signal">Signal payload (JSON)</label>
                  <textarea id="workflow-signal" value={signalPayload}
                    onChange={(event) => setSignalPayload(event.target.value)} />
                  {signalError && <p role="alert">{signalError}</p>}
                  <button type="submit" disabled={actions.signal.isPending}>Deliver signal</button>
                </form>
              )}
              {detail.recovery.needs_attention && (
                <div role="alert" aria-label="Recovery guidance">
                  <p>{detail.recovery.summary}</p>
                  {detail.recovery.guidance && <p>{detail.recovery.guidance}</p>}
                </div>
              )}

              <h3>Steps</h3>
              <ol aria-label="Workflow steps">
                {detail.steps.map((step) => (
                  <li key={step.step_number}>
                    Step {step.step_number}: {step.status}
                    {step.run_id !== null && (
                      <button type="button" onClick={() => navigate(`/runs/${step.run_id}`)}>
                        Run {step.run_id}
                      </button>
                    )}
                    {step.summary && <span>{step.summary}</span>}
                  </li>
                ))}
              </ol>

              <h3>Checkpoints</h3>
              <p className="hint">
                Checkpoint contents stay on the server until you choose Inspect.
                Private state is shown as text and never used as permissions.
              </p>
              <ul aria-label="Workflow checkpoints">
                {detail.checkpoints.map((checkpoint) => (
                  <li key={checkpoint.revision}>
                    Revision {checkpoint.revision} from step {checkpoint.step_number}:{" "}
                    {checkpoint.byte_size} bytes, keys {checkpoint.keys.join(", ") || "none"}
                    <WorkflowCheckpoint workflowId={detail.workflow.id} revision={checkpoint.revision} />
                  </li>
                ))}
              </ul>

              {detail.decisions.length > 0 && (
                <>
                  <h3>Proposed actions</h3>
                  <ul aria-label="Pending decisions">
                    {detail.decisions.map((decision) => (
                      <li key={decision.id}>
                        <span>{decision.upstream_name}</span>
                        <span>{decision.state}</span>
                        <p>Expires {formatTime(decision.expires_at)}</p>
                        <pre aria-label="Action preview">{JSON.stringify(decision.preview, null, 2)}</pre>
                        <p className="hint">Approval permits the proposed next step. Tool permissions and account access are checked again at execution.</p>
                        {decision.state === "pending" && (
                          <>
                            <button
                              type="button"
                              disabled={actions.decide.isPending}
                              onClick={() =>
                                actions.decide.mutate({
                                  id: detail.workflow.id,
                                  decisionId: decision.id,
                                  approve: true,
                                  expectedRevision: decision.checkpoint_revision,
                                })
                              }
                            >
                              Approve
                            </button>
                            <button
                              type="button"
                              disabled={actions.decide.isPending}
                              onClick={() =>
                                actions.decide.mutate({
                                  id: detail.workflow.id,
                                  decisionId: decision.id,
                                  approve: false,
                                  expectedRevision: decision.checkpoint_revision,
                                })
                              }
                            >
                              Decline
                            </button>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </section>
      )}
    </main>
  );
}
