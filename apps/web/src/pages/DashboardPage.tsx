import { useLogout } from "../api/queries";
import type { User } from "../api/types";
import { Brand } from "../components/Brand";
import { InlineError } from "../components/AsyncState";
import { Link, useNavigate } from "react-router-dom";

export function DashboardPage({ user }: { user: User }) {
  const logout = useLogout();
  const navigate = useNavigate();

  async function handleLogout() {
    try {
      await logout.mutateAsync();
      await navigate("/login", { replace: true });
    } catch {
      // A failed logout leaves the authenticated dashboard in place.
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <Brand />
        <div className="account-actions">
          <span>{user.username}</span>
          <button className="secondary" type="button" onClick={() => void handleLogout()} disabled={logout.isPending}>
            {logout.isPending ? "Signing out…" : "Log out"}
          </button>
        </div>
      </header>
      <section className="dashboard-content" aria-labelledby="dashboard-title">
        <p className="eyebrow">Control plane</p>
        <h1 id="dashboard-title">NervOS is ready.</h1>
        <p className="lede">Your self-hosted agent platform is installed and this administrator session is active.</p>
        {logout.error ? <InlineError error={logout.error} /> : null}
        <div className="panel ready-card">
          <div className="ready-indicator" aria-hidden="true" />
          <div>
            <h2>Trusted chat is available</h2>
            <p>
              Create a Chat agent and run it here. Every submission is one independent run that
              keeps its own result.
            </p>
            <Link className="button-link" to="/agents">
              Open agents
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}
