import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { agentInstancesQuery, useCreateAgentInstance } from "../api/queries";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";

// The single trusted definition this milestone exposes. It is never chosen implicitly.
const DEFINITION_KEY = "nervos.chat";
const DEFINITION_VERSION = "1";
const PROVIDER_ID = "anthropic";

export function AgentInstancesPage() {
  const instances = useQuery(agentInstancesQuery());
  const createInstance = useCreateAgentInstance();
  const navigate = useNavigate();
  const [showForm, setShowForm] = useState(false);

  async function handleCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const created = await createInstance.mutateAsync({
        agent_key: DEFINITION_KEY,
        agent_definition_version: DEFINITION_VERSION,
        display_name: String(data.get("display_name") ?? ""),
        model_provider: PROVIDER_ID,
        model_name: String(data.get("model_name") ?? ""),
      });
      await navigate(`/agents/${created.id}`);
    } catch {
      // Rendered from the mutation's error state below.
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <Brand />
        <div className="account-actions">
          <Link className="button-link secondary" to="/dashboard">
            Dashboard
          </Link>
        </div>
      </header>

      <section className="dashboard-content" aria-labelledby="agents-title">
        <p className="eyebrow">Trusted chat</p>
        <h1 id="agents-title">Agents</h1>
        <p className="lede">
          Each agent is an explicit instance you own. Every submission runs independently and keeps
          its own result.
        </p>

        {instances.isPending ? <LoadingState message="Loading agents…" /> : null}

        {instances.isError ? (
          <ErrorState error={instances.error} onRetry={() => void instances.refetch()} />
        ) : null}

        {instances.data !== undefined && instances.data.items.length === 0 ? (
          <div className="panel empty-card">
            <h2>No agents yet</h2>
            <p>
              Create your first Chat agent to run the trusted <code>nervos.chat</code> version 1
              behavior. Nothing is created for you automatically.
            </p>
            <button type="button" onClick={() => setShowForm(true)}>
              Create a Chat agent
            </button>
          </div>
        ) : null}

        {instances.data !== undefined && instances.data.items.length > 0 ? (
          <>
            <ul className="instance-list">
              {instances.data.items.map((instance) => (
                <li key={instance.id}>
                  <Link className="instance-link panel" to={`/agents/${instance.id}`}>
                    <span className="instance-name">{instance.display_name}</span>
                    <span className="instance-detail">
                      {instance.agent_key} v{instance.agent_definition_version} ·{" "}
                      {instance.model_provider} · {instance.model_name}
                    </span>
                    <span
                      className={instance.enabled ? "instance-state" : "instance-state disabled"}
                    >
                      {instance.enabled ? "Enabled" : "Disabled"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
            {showForm ? null : (
              <button type="button" onClick={() => setShowForm(true)}>
                Create another Chat agent
              </button>
            )}
          </>
        ) : null}

        {showForm ? (
          <section className="panel create-panel" aria-labelledby="create-agent-title">
            <h2 id="create-agent-title">Create a Chat agent</h2>
            <form onSubmit={(event) => void handleCreate(event)}>
              <label htmlFor="display_name">Display name</label>
              <input
                id="display_name"
                name="display_name"
                type="text"
                required
                maxLength={100}
                autoComplete="off"
              />
              <p className="field-hint">A label for you. It does not have to be unique.</p>

              <label htmlFor="agent_definition">Agent definition</label>
              <input
                id="agent_definition"
                type="text"
                value={`${DEFINITION_KEY} v${DEFINITION_VERSION}`}
                readOnly
                aria-describedby="agent_definition_hint"
              />
              <p className="field-hint" id="agent_definition_hint">
                The exact trusted definition this milestone supports. It cannot be changed.
              </p>

              <label htmlFor="model_provider">Model provider</label>
              <input id="model_provider" type="text" value={PROVIDER_ID} readOnly />
              <p className="field-hint">Anthropic is currently the only supported provider.</p>

              <label htmlFor="model_name">Model</label>
              <input
                id="model_name"
                name="model_name"
                type="text"
                required
                maxLength={256}
                autoComplete="off"
                aria-describedby="model_name_hint"
              />
              <p className="field-hint" id="model_name_hint">
                Enter the provider&apos;s model identifier exactly. NervOS does not check whether it
                exists until you run the agent, and an unavailable model fails safely.
              </p>

              <button type="submit" disabled={createInstance.isPending}>
                {createInstance.isPending ? "Creating…" : "Create agent"}
              </button>
            </form>
            {createInstance.error ? <InlineError error={createInstance.error} /> : null}
          </section>
        ) : null}
      </section>
    </main>
  );
}
