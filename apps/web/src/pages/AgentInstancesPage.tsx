import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { MODEL_PROVIDERS } from "../api/providers";
import { packagesQuery } from "../api/packageQueries";
import { agentInstancesQuery, useCreateAgentInstance } from "../api/queries";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";
import { ConfigSchemaForm } from "../components/ConfigSchemaForm";

const BUILTIN_KEY = "nervos.chat";
const BUILTIN_VERSION = "1";

export function AgentInstancesPage() {
  const instances = useQuery(agentInstancesQuery());
  const activePackages = useQuery(packagesQuery("active"));
  const createInstance = useCreateAgentInstance();
  const navigate = useNavigate();
  const [showForm, setShowForm] = useState(false);

  const [selectedDefinition, setSelectedDefinition] = useState("nervos.chat v1");
  const [packageConfig, setPackageConfig] = useState<Record<string, unknown>>({});

  const isPackage = selectedDefinition !== "nervos.chat v1";
  const [selectedKey, selectedVersion] = isPackage
    ? selectedDefinition.split("@")
    : [BUILTIN_KEY, BUILTIN_VERSION];

  async function handleCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const payload: {
        agent_key: string;
        agent_definition_version: string;
        display_name: string;
        model_provider: string;
        model_name: string;
        package_config?: Record<string, unknown>;
      } = {
        agent_key: selectedKey,
        agent_definition_version: selectedVersion,
        display_name: String(data.get("display_name") ?? ""),
        model_provider: String(data.get("model_provider") ?? ""),
        model_name: String(data.get("model_name") ?? ""),
      };

      if (isPackage) {
        payload.package_config = packageConfig;
      }

      const created = await createInstance.mutateAsync(payload);
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
          <Link className="button-link secondary" to="/packages">
            Packages
          </Link>
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
              <select
                id="agent_definition"
                value={selectedDefinition}
                onChange={(e) => setSelectedDefinition(e.target.value)}
              >
                <option value="nervos.chat v1">
                  nervos.chat v1
                </option>
                {activePackages.data?.items.map((pkg) => (
                  <option key={`${pkg.package_id}@${pkg.package_version}`} value={`${pkg.package_id}@${pkg.package_version}`}>
                    {pkg.display_name} ({pkg.package_id}@{pkg.package_version})
                  </option>
                ))}
              </select>
              <p className="field-hint">
                Choose a built-in agent or an active installed package.
              </p>

              <label htmlFor="model_provider">Model provider</label>
              <select id="model_provider" name="model_provider" defaultValue="anthropic">
                {MODEL_PROVIDERS.map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.label}
                  </option>
                ))}
              </select>
              <p className="field-hint">Choose a supported provider. Credentials stay in the API process.</p>

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

              {isPackage && (
                <div className="pt-2 border-t dark:border-gray-700">
                  <ConfigSchemaForm
                    schema={{}}
                    initialConfig={packageConfig}
                    onChange={setPackageConfig}
                  />
                </div>
              )}

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
