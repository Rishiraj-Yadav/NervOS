import type { Run } from "../api/agentInstances";

const STATUS_LABELS: Record<Run["status"], string> = {
  created: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
};

function usageLine(run: Run): string | null {
  if (run.usage === null) {
    return null;
  }
  const parts: string[] = [];
  if (run.usage.input_tokens !== null) {
    parts.push(`${run.usage.input_tokens} in`);
  }
  if (run.usage.output_tokens !== null) {
    parts.push(`${run.usage.output_tokens} out`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

/**
 * One independent persisted Run.
 *
 * Everything is rendered as plain React text, so model output is escaped by React and can never
 * become markup. Only a durably persisted Run is ever shown; there is no fabricated answer.
 */
export function RunItem({ run }: { run: Run }) {
  const usage = usageLine(run);

  return (
    <article className="run-item">
      <header className="run-head">
        <span className={`run-status run-status-${run.status}`}>{STATUS_LABELS[run.status]}</span>
        <span className="run-meta">Run #{run.id}</span>
        <span className="run-meta">
          {run.model_provider} · {run.model_name}
        </span>
        {run.elapsed_ms !== null ? <span className="run-meta">{run.elapsed_ms} ms</span> : null}
        {usage !== null ? <span className="run-meta">{usage}</span> : null}
      </header>

      <p className="run-label">Prompt</p>
      <p className="run-text">{run.input_text}</p>

      {run.status === "succeeded" && run.output_text !== null ? (
        <>
          <p className="run-label">Response</p>
          <p className="run-text">{run.output_text}</p>
        </>
      ) : null}

      {run.status === "failed" ? (
        <p className="run-failure" role="note">
          {run.error_message}
          {run.error_code !== null ? ` (${run.error_code})` : ""}
        </p>
      ) : null}

      {run.status === "created" ? (
        <p className="run-pending" role="note">
          Accepted and queued. A worker must be running to execute this run.
        </p>
      ) : null}

      {run.status === "running" ? (
        <p className="run-pending" role="note">
          Running. If the worker stops before it finishes, this run will not complete; NervOS
          does not yet recover or retry it.
        </p>
      ) : null}
    </article>
  );
}
