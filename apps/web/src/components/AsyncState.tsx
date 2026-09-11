interface LoadingStateProps {
  message?: string;
}

export function LoadingState({ message = "Loading NervOS…" }: LoadingStateProps) {
  return (
    <main className="centered-page" aria-busy="true">
      <section className="panel status-panel" aria-live="polite" role="status" aria-label="Loading NervOS">
        <span className="spinner" aria-hidden="true" />
        <p>{message}</p>
      </section>
    </main>
  );
}

interface ErrorStateProps {
  title?: string;
  error: unknown;
  onRetry: () => void;
}

export function ErrorState({
  title = "NervOS could not load",
  error,
  onRetry,
}: ErrorStateProps) {
  const message = error instanceof Error ? error.message : "An unexpected error occurred.";
  return (
    <main className="centered-page">
      <section className="panel status-panel" role="alert">
        <p className="eyebrow">Connection problem</p>
        <h1>{title}</h1>
        <p>{message}</p>
        <button type="button" onClick={onRetry}>Try again</button>
      </section>
    </main>
  );
}

export function InlineError({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : "An unexpected error occurred.";
  return <p className="form-error" role="alert">{message}</p>;
}
