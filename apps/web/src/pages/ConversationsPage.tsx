import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  conversationsListQuery,
  useConversationActions,
} from "../api/conversationQueries";
import { agentInstancesQuery } from "../api/queries";
import { ErrorState, LoadingState } from "../components/AsyncState";

export function ConversationsPage() {
  const navigate = useNavigate();
  const listQ = useQuery(conversationsListQuery());
  const agentsQ = useQuery(agentInstancesQuery());
  const actions = useConversationActions();

  const [selectedAgent, setSelectedAgent] = useState<number | "">("");
  const [title, setTitle] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  if (listQ.isPending || agentsQ.isPending) return <LoadingState />;
  if (listQ.isError)
    return <ErrorState error={listQ.error} onRetry={() => void listQ.refetch()} />;
  if (agentsQ.isError)
    return <ErrorState error={agentsQ.error} onRetry={() => void agentsQ.refetch()} />;

  const agents = agentsQ.data.items;

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedAgent) return;
    actions.createConversation.mutate(
      {
        agent_instance_id: Number(selectedAgent),
        title: title.trim() || undefined,
      },
      {
        onSuccess: (data) => {
          navigate(`/conversations/${data.id}`);
        },
      }
    );
  };

  return (
    <main className="page-shell">
      <header className="page-header">
        <div>
          <p className="eyebrow">Control plane</p>
          <h1>Conversations</h1>
          <p>Interactive multi-turn conversation sessions with your agents.</p>
        </div>
      </header>

      <section className="panel page-card">
        <div className="section-heading">
          <h2>All conversations</h2>
          {!showCreate && (
            <button
              className="button-link"
              onClick={() => {
                setShowCreate(true);
                if (agents.length > 0 && selectedAgent === "") {
                  setSelectedAgent(agents[0].id);
                }
              }}
            >
              New conversation
            </button>
          )}
        </div>

        {showCreate && (
          <form className="form-card" onSubmit={handleCreate}>
            <h3>Start a new conversation</h3>
            <label>
              Agent
              <select
                value={selectedAgent}
                onChange={(e) => setSelectedAgent(Number(e.target.value))}
                required
              >
                <option value="" disabled>
                  Select an agent...
                </option>
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.display_name} ({a.agent_key}@{a.agent_definition_version})
                  </option>
                ))}
              </select>
            </label>

            <label>
              Title (optional)
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="New conversation"
                maxLength={100}
              />
            </label>

            <div className="button-row">
              <button
                type="submit"
                disabled={actions.createConversation.isPending || !selectedAgent}
              >
                {actions.createConversation.isPending ? "Creating..." : "Start"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => setShowCreate(false)}
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        {listQ.data.items.length === 0 ? (
          <p>No conversations yet.</p>
        ) : (
          <div className="stack">
            {listQ.data.items.map((c) => (
              <article className="list-row" key={c.id}>
                <div>
                  <Link to={`/conversations/${c.id}`}>
                    <strong>{c.title || "New conversation"}</strong>
                  </Link>
                  <p>
                    Agent {c.agent_instance_id} · Created{" "}
                    {new Date(c.created_at).toLocaleString()}
                  </p>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
