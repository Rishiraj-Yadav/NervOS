import { ApiHttpError } from "../api/client";
import { queryKeys, reconcileSetupComplete, useSetup } from "../api/queries";
import { Brand } from "../components/Brand";
import { InlineError } from "../components/AsyncState";
import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

export function SetupPage() {
  const setup = useSetup();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [confirmationError, setConfirmationError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setConfirmationError(null);
    const data = new FormData(form);
    const username = String(data.get("username") ?? "");
    const password = String(data.get("password") ?? "");
    const confirmation = String(data.get("confirmation") ?? "");
    if (password !== confirmation) {
      setConfirmationError("Passwords do not match.");
      return;
    }
    try {
      await setup.mutateAsync({ username, password });
      await navigate("/", { replace: true });
    } catch (error) {
      if (error instanceof ApiHttpError && error.status === 409 && error.code === "setup_complete") {
        queryClient.setQueryData(queryKeys.setup, { setup_complete: true });
        try {
          await reconcileSetupComplete(queryClient);
        } catch {
          // The route gate presents the authoritative authentication result.
        }
        await navigate("/", { replace: true });
        return;
      }
      const passwordInput = form.elements.namedItem("password");
      if (passwordInput instanceof HTMLInputElement) {
        passwordInput.focus();
      }
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-intro">
        <Brand />
        <div>
          <p className="eyebrow">First-run setup</p>
          <h1>Your agents.<br />Your hardware.<br /><em>Your rules.</em></h1>
          <p>Create the local administrator account that will control this NervOS installation.</p>
        </div>
      </section>
      <section className="panel auth-panel" aria-labelledby="setup-title">
        <p className="step">Step 1 of 1</p>
        <h2 id="setup-title">Create your administrator</h2>
        <p>This account stays on your server.</p>
        <form onSubmit={(event) => void handleSubmit(event)}>
          <label htmlFor="username">Username</label>
          <input id="username" name="username" autoComplete="username" required maxLength={128} />
          <label htmlFor="password">Password</label>
          <input id="password" name="password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} />
          <p className="field-hint">Use at least 12 characters.</p>
          <label htmlFor="confirmation">Confirm password</label>
          <input id="confirmation" name="confirmation" type="password" autoComplete="new-password" required minLength={12} maxLength={128} aria-describedby={confirmationError ? "confirmation-error" : undefined} />
          {confirmationError ? <p id="confirmation-error" className="form-error" role="alert">{confirmationError}</p> : null}
          {setup.error ? <InlineError error={setup.error} /> : null}
          <button type="submit" disabled={setup.isPending}>{setup.isPending ? "Creating account…" : "Create administrator"}</button>
        </form>
      </section>
    </main>
  );
}
