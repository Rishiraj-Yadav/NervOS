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
  const [statusFilter, setStatusFilter] = useState<"active" | "archived">("active");
  const listQ = useQuery(conversationsListQuery(undefined, statusFilter));
  const agentsQ = useQuery(agentInstancesQuery());
  const actions = useConversationActions();

  const [selectedAgent, setSelectedAgent] = useState<number | "">("");
  const [title, setTitle] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);

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

  const handleDeleteConfirm = () => {
    if (deletingId === null) return;
    actions.deleteConversation.mutate(deletingId, {
      onSuccess: () => {
        setDeletingId(null);
      },
    });
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
          <div className="tab-group" role="tablist">
            <button
              className={`button-link ${statusFilter === "active" ? "active font-bold" : ""}`}
              onClick={() => setStatusFilter("active")}
              role="tab"
              aria-selected={statusFilter === "active"}
            >
              Active
            </button>
            <button
              className={`button-link ${statusFilter === "archived" ? "active font-bold" : ""}`}
              onClick={() => setStatusFilter("archived")}
              role="tab"
              aria-selected={statusFilter === "archived"}
            >
              Archived
            </button>
          </div>

          {!showCreate && statusFilter === "active" && (
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
          <form className="form-card" onSubmit={handleCreate} style={{ marginTop: "1rem" }}>
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
              />
            </label>

            <div className="form-actions" style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
              <button type="submit" disabled={actions.createConversation.isPending}>
                {actions.createConversation.isPending
                  ? "Starting..."
                  : "Start conversation"}
              </button>
              <button type="button" onClick={() => setShowCreate(false)}>
                Cancel
              </button>
            </div>
          </form>
        )}

        {listQ.data.items.length === 0 ? (
          <p className="empty-state" style={{ marginTop: "1rem" }}>
            {statusFilter === "active" ? "No active conversations." : "No archived conversations."}
          </p>
        ) : (
          <div className="stack" style={{ marginTop: "1rem" }}>
            {listQ.data.items.map((c) => (
              <article
                className="list-row"
                key={c.id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <div>
                  <Link to={`/conversations/${c.id}`}>
                    <strong>{c.title || "New conversation"}</strong>
                  </Link>
                  <p style={{ margin: "0.25rem 0 0 0", fontSize: "0.875rem", color: "var(--muted, #64748b)" }}>
                    Agent {c.agent_instance_id} · Created{" "}
                    {new Date(c.created_at).toLocaleString()}
                    {c.archived_at && ` · Archived ${new Date(c.archived_at).toLocaleString()}`}
                  </p>
                </div>

                <div style={{ display: "flex", gap: "0.5rem" }}>
                  {c.status === "active" ? (
                    <button
                      type="button"
                      className="button-link"
                      onClick={() => actions.archiveConversation.mutate(c.id)}
                      disabled={actions.archiveConversation.isPending}
                    >
                      Archive
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="button-link"
                      onClick={() => actions.unarchiveConversation.mutate(c.id)}
                      disabled={actions.unarchiveConversation.isPending}
                    >
                      Unarchive
                    </button>
                  )}
                  <button
                    type="button"
                    className="button-link text-danger"
                    onClick={() => setDeletingId(c.id)}
                  >
                    Delete
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {/* Delete Confirmation Modal */}
      {deletingId !== null && (
        <div className="modal-backdrop" style={{ marginTop: "1.5rem" }}>
          <section className="panel form-card">
            <h3>Delete conversation #{deletingId}</h3>
            <p>
              Are you sure you want to delete this conversation? It will be removed from active
              chat sessions.
            </p>
            <p className="text-secondary" style={{ fontSize: "0.875rem" }}>
              Note: Historical execution logs and snapshots will be retained in Runs.
            </p>
            <div className="form-actions" style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
              <button
                type="button"
                className="button-danger"
                onClick={handleDeleteConfirm}
                disabled={actions.deleteConversation.isPending}
              >
                {actions.deleteConversation.isPending ? "Deleting..." : "Confirm delete"}
              </button>
              <button type="button" onClick={() => setDeletingId(null)}>
                Cancel
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
