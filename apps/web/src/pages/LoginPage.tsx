import { useLogin } from "../api/queries";
import { Brand } from "../components/Brand";
import { InlineError } from "../components/AsyncState";
import { type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

export function LoginPage() {
  const login = useLogin();
  const navigate = useNavigate();
  const location = useLocation();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await login.mutateAsync({
        username: String(data.get("username") ?? ""),
        password: String(data.get("password") ?? ""),
      });
      const destination = getReturnPath(location.state);
      await navigate(destination, { replace: true });
    } catch {
      const password = form.elements.namedItem("password");
      if (password instanceof HTMLInputElement) {
        password.value = "";
        password.focus();
      }
    }
  }

  return (
    <main className="centered-page">
      <section className="login-wrap">
        <Brand />
        <div className="panel auth-panel" aria-labelledby="login-title">
          <p className="eyebrow">Local control plane</p>
          <h1 id="login-title">Welcome back</h1>
          <p>Sign in to manage this NervOS installation.</p>
          <form onSubmit={(event) => void handleSubmit(event)}>
            <label htmlFor="username">Username</label>
            <input id="username" name="username" autoComplete="username" required maxLength={128} />
            <label htmlFor="password">Password</label>
            <input id="password" name="password" type="password" autoComplete="current-password" required minLength={12} maxLength={128} />
            {login.error ? <InlineError error={login.error} /> : null}
            <button type="submit" disabled={login.isPending}>{login.isPending ? "Signing in…" : "Sign in"}</button>
          </form>
        </div>
      </section>
    </main>
  );
}

function getReturnPath(state: unknown): string {
  if (typeof state !== "object" || state === null || !("from" in state)) {
    return "/";
  }
  const from = state.from;
  return typeof from === "string" && from.startsWith("/") ? from : "/";
}
