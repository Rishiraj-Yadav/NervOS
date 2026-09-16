import { useRunEvents } from "../api/queries";
import { formatInstant, type RunEvent, type RunEventType } from "../api/agentInstances";
import { InlineError } from "./AsyncState";

interface EventCopy {
  headline: (event: RunEvent) => string;
  detail: (event: RunEvent) => string | null;
  /** True when the headline already names the attempt, so no separate label is added. */
  namesAttempt?: boolean;
}

function attemptPhrase(event: RunEvent, rest: string): string {
  return event.attempt_number === null
    ? `Attempt ${rest}`
    : `Attempt ${event.attempt_number} ${rest}`;
}

/** The sanitized error pair, or nothing at all. Never a raw provider payload. */
function safeFailure(event: RunEvent): string | null {
  if (event.message === null) {
    return null;
  }
  return event.code === null ? event.message : `${event.message} (${event.code})`;
}

/**
 * The whole Event vocabulary, mapped at compile time, so a new server-side type cannot render as a
 * raw enum or as a blank row: the record is total over `RunEventType`, and adding a type without a
 * case fails the frontend's own type check.
 *
 * Four rules hold across every entry. No headline claims provider-side certainty. No headline names
 * a provider as a cause. Nothing says a Worker "died", "crashed", or "was killed", because a stale
 * lease and a paused process are indistinguishable by design and only the durable lease event was
 * ever observed. And a timeout is never described as a cancellation, nor the reverse.
 */
const EVENT_COPY: Record<RunEventType, EventCopy> = {
  "run.created": {
    headline: () => "Run accepted",
    detail: () => null,
  },
  "run.queued": {
    headline: () => "Queued for a worker",
    detail: () => null,
  },
  "attempt.claimed": {
    headline: () => "A worker claimed this run",
    detail: () => null,
  },
  "attempt.started": {
    headline: () => "Execution started",
    detail: () => null,
  },
  "attempt.failed": {
    headline: (event) => attemptPhrase(event, "failed"),
    detail: safeFailure,
    namesAttempt: true,
  },
  "attempt.expired": {
    headline: (event) => attemptPhrase(event, "lease expired"),
    detail: () => null,
    namesAttempt: true,
  },
  "retry.scheduled": {
    headline: () => "Retry scheduled",
    detail: (event) =>
      event.available_at === null
        ? "Another attempt will run when its backoff elapses."
        : `Waiting until ${formatInstant(event.available_at)}.`,
  },
  "recovery.pre_start": {
    headline: () => "Recovered before execution began",
    detail: () => "The worker did not begin this attempt, so NervOS queued it again.",
  },
  "recovery.ambiguous": {
    headline: () => "Execution outcome unknown",
    detail: (event) =>
      `${safeFailure(event) ?? ""} The result of this attempt could not be determined, so it was not replayed.`.trim(),
  },
  "cancellation.requested": {
    headline: () => "Cancellation requested",
    detail: () => null,
  },
  "run.cancelled": {
    headline: () => "Run cancelled",
    detail: () => "NervOS stopped waiting for this run. A request already sent may still have been processed.",
  },
  "run.succeeded": {
    headline: () => "Run succeeded",
    detail: () => null,
  },
  "run.failed": {
    headline: () => "Run failed",
    detail: (event) => {
      const failure = safeFailure(event);
      if (event.code === "model_timed_out") {
        // A timeout is a failure, never a cancellation: NervOS stopped waiting but revoked
        // nothing, so it must not borrow the cancellation wording.
        return `${failure} NervOS stopped waiting after the execution limit, and the provider's outcome is unknown, so this run was not retried.`;
      }
      return failure;
    },
  },
};

function TimelineRow({ event }: { event: RunEvent }) {
  const copy = EVENT_COPY[event.event_type];
  const detail = copy.detail(event);
  const showAttemptLabel = event.attempt_number !== null && copy.namesAttempt !== true;

  return (
    <li className={`run-timeline-step run-timeline-${event.event_type.replace(".", "-")}`}>
      <div className="run-timeline-head">
        <span className="run-timeline-headline">{copy.headline(event)}</span>
        {showAttemptLabel ? (
          <span className="run-timeline-attempt">Attempt {event.attempt_number}</span>
        ) : null}
        <span className="run-timeline-time">{formatInstant(event.created_at)}</span>
      </div>
      {detail !== null ? <p className="run-timeline-detail">{detail}</p> : null}
    </li>
  );
}

/**
 * One Run's durable execution timeline.
 *
 * Every row is a durably persisted fact rendered as plain React text, so nothing here can become
 * markup and nothing is inferred: the timeline reports what was recorded, never a speculation about
 * why. It fetches only while the Run is being observed, and once the Run is terminal it performs a
 * single catch-up cycle before going quiet for good.
 */
export function RunTimeline({ runId, isTerminal }: { runId: number; isTerminal: boolean }) {
  const { data, isPending, isError, error, refetch } = useRunEvents(runId, isTerminal);

  if (isError) {
    return <InlineError error={error} />;
  }
  // A Run always has at least two Events, so an empty timeline means "not read yet", not "none".
  if (isPending || data === undefined || data.length === 0) {
    return (
      <p className="run-timeline-note" aria-live="polite">
        Loading execution timeline…
      </p>
    );
  }

  return (
    <>
      <ol className="run-timeline" aria-label={`Execution timeline for run ${runId}`}>
        {data.map((event) => (
          <TimelineRow key={event.sequence} event={event} />
        ))}
      </ol>
      <button type="button" className="run-timeline-refresh" onClick={() => void refetch()}>
        Refresh timeline
      </button>
    </>
  );
}
