import { useLogin } from "../api/queries";
import { Brand } from "../components/Brand";
import { InlineError } from "../components/AsyncState";
import { type FormEvent } from "react";

export function LoginPage() {
  const login = useLogin();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      // The session write and the resulting route change are both derived from the gate: a
      // navigate() here would race the write and redirect the freshly authenticated session back
      // out of the page it just left.
      await login.mutateAsync({
        username: String(data.get("username") ?? ""),
        password: String(data.get("password") ?? ""),
      });
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
