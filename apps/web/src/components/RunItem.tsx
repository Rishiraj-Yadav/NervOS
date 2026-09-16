import type { ReactNode } from "react";
import { useState } from "react";

import { formatInstant, type Run } from "../api/agentInstances";

const STATUS_LABELS: Record<Run["status"], string> = {
  created: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
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
 *
 * Cancellation is offered only while the Run is still nonterminal, and only when the page
 * supplies the action: this component stays presentational and owns no request of its own. The
 * timeline it reveals follows the same rule -- the element is supplied by the page and is only
 * mounted once the reader asks for it, so a collapsed Run costs no request.
 */
export function RunItem({
  run,
  onCancel,
  isCancelling = false,
  timeline,
}: {
  run: Run;
  onCancel?: (runId: number) => void;
  isCancelling?: boolean;
  timeline?: ReactNode;
}) {
  const usage = usageLine(run);
  const cancellable = (run.status === "created" || run.status === "running") && onCancel !== undefined;
  const [showTimeline, setShowTimeline] = useState(false);
  const timelineId = `run-timeline-${run.id}`;

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
        {cancellable ? (
          <button
            type="button"
            className="run-cancel"
            onClick={() => onCancel(run.id)}
            disabled={isCancelling}
          >
            {isCancelling ? "Cancelling…" : "Cancel"}
          </button>
        ) : null}
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

      {run.status === "running" && run.execution_phase === "retry_wait" ? (
        <p className="run-pending" role="note">
          Waiting to retry. The last attempt ended with an outcome the provider positively declined,
          and another attempt is scheduled for {formatInstant(run.retry_available_at)}. Nothing
          promises that the next attempt will succeed.
        </p>
      ) : null}

      {run.status === "running" && run.execution_phase === "claimed" ? (
        <p className="run-pending" role="note">
          A worker has claimed this run and has not yet begun the model request.
        </p>
      ) : null}

      {run.status === "running" &&
      run.execution_phase !== "retry_wait" &&
      run.execution_phase !== "claimed" ? (
        <p className="run-pending" role="note">
          Running. If the worker stops before it finishes, NervOS closes this run as failed
          without replaying it, because the model request may already have been sent. A failure
          the provider positively declined may be retried, so this run can be waiting briefly
          before it executes again.
        </p>
      ) : null}

      {run.status === "cancelled" ? (
        <p className="run-pending" role="note">
          Cancelled. NervOS stopped waiting for this run and will not execute it again. A request
          that had already been sent may still have been processed by the provider, so
          cancellation cannot promise that the provider stopped or that nothing was billed.
        </p>
      ) : null}

      {timeline !== undefined ? (
        <div className="run-timeline-panel">
          <button
            type="button"
            className="run-timeline-toggle"
            aria-expanded={showTimeline}
            aria-controls={timelineId}
            onClick={() => setShowTimeline((expanded) => !expanded)}
          >
            {showTimeline ? "Hide timeline" : "Show timeline"}
          </button>
          {showTimeline ? (
            <div id={timelineId} className="run-timeline-body">
              {timeline}
            </div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
