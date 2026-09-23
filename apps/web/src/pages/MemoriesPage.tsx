import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  memoriesListQuery,
  memoryVersionsQuery,
  useMemoryActions,
} from "../api/memoryQueries";
import { agentInstancesQuery } from "../api/queries";
import { MemoryItem, MemoryVersionItem } from "../api/memories";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";
import { ApiHttpError } from "../api/client";

export function MemoriesPage() {
  const [scopeFilter, setScopeFilter] = useState<"all" | "user" | "agent">("all");
  const [selectedAgent, setSelectedAgent] = useState<number | "">("");
  const [showCreate, setShowCreate] = useState(false);
  const [editingMemory, setEditingMemory] = useState<MemoryItem | null>(null);
  const [viewingHistoryId, setViewingHistoryId] = useState<number | null>(null);
  const [deletingMemory, setDeletingMemory] = useState<MemoryItem | null>(null);

  // Form states
  const [createScope, setCreateScope] = useState<"user" | "agent">("user");
  const [createAgentId, setCreateAgentId] = useState<number | "">("");
  const [createContent, setCreateContent] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editError, setEditError] = useState<string | null>(null);

  const scopeParam = scopeFilter === "all" ? undefined : scopeFilter;
  const agentParam =
    scopeFilter === "agent" && selectedAgent !== "" ? Number(selectedAgent) : undefined;

  const listQ = useQuery(memoriesListQuery(scopeParam, agentParam));
  const agentsQ = useQuery(agentInstancesQuery());
  const historyQ = useQuery({
    ...memoryVersionsQuery(viewingHistoryId ?? 0),
    enabled: viewingHistoryId !== null,
  });

  const actions = useMemoryActions();

  if (listQ.isPending || agentsQ.isPending) return <LoadingState />;
  if (listQ.isError)
    return <ErrorState error={listQ.error} onRetry={() => void listQ.refetch()} />;
  if (agentsQ.isError)
    return <ErrorState error={agentsQ.error} onRetry={() => void agentsQ.refetch()} />;

  const agents = agentsQ.data.items;
  const memories = listQ.data.items;

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!createContent.trim()) return;
    if (createScope === "agent" && !createAgentId) return;

    actions.createMemory.mutate(
      {
        scope: createScope,
        content: createContent.trim(),
        agent_instance_id: createScope === "agent" ? Number(createAgentId) : undefined,
      },
      {
        onSuccess: () => {
          setShowCreate(false);
          setCreateContent("");
        },
      }
    );
  };

  const handleStartEdit = (mem: MemoryItem) => {
    setEditingMemory(mem);
    setEditContent(mem.content);
    setEditError(null);
  };

  const handleSaveEdit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingMemory || !editContent.trim()) return;

    actions.editMemory.mutate(
      {
        id: editingMemory.id,
        body: {
          expected_version: editingMemory.current_version,
          content: editContent.trim(),
        },
      },
      {
        onSuccess: () => {
          setEditingMemory(null);
          setEditContent("");
          setEditError(null);
        },
        onError: (err) => {
          if (err instanceof ApiHttpError && err.status === 409) {
            setEditError(
              "This memory was modified elsewhere. The latest version has been refreshed."
            );
            void listQ.refetch();
          } else {
            setEditError(err instanceof Error ? err.message : "Failed to update memory.");
          }
        },
      }
    );
  };

  const handleDelete = () => {
    if (!deletingMemory) return;
    actions.deleteMemory.mutate(
      {
        id: deletingMemory.id,
        expectedVersion: deletingMemory.current_version,
      },
      {
        onSuccess: () => {
          setDeletingMemory(null);
        },
      }
    );
  };

  return (
    <main className="page-shell">
      <header className="page-header">
        <div>
          <p className="eyebrow">Control plane</p>
          <h1>Scoped Memory</h1>
          <p>Inspect, edit, and delete durable USER and AGENT scoped facts.</p>
        </div>
      </header>

      <section className="panel page-card">
        <div className="section-heading">
          <div className="tab-group" role="tablist">
            <button
              className={`button-link ${scopeFilter === "all" ? "active font-bold" : ""}`}
              onClick={() => setScopeFilter("all")}
              role="tab"
              aria-selected={scopeFilter === "all"}
            >
              All memories
            </button>
            <button
              className={`button-link ${scopeFilter === "user" ? "active font-bold" : ""}`}
              onClick={() => setScopeFilter("user")}
              role="tab"
              aria-selected={scopeFilter === "user"}
            >
              User profile
            </button>
            <button
              className={`button-link ${scopeFilter === "agent" ? "active font-bold" : ""}`}
              onClick={() => setScopeFilter("agent")}
              role="tab"
              aria-selected={scopeFilter === "agent"}
            >
              Agent private
            </button>
          </div>

          {!showCreate && (
            <button
              className="button-link"
              onClick={() => {
                setShowCreate(true);
                if (agents.length > 0 && createAgentId === "") {
                  setCreateAgentId(agents[0].id);
                }
              }}
            >
              Add memory
            </button>
          )}
        </div>

        {scopeFilter === "agent" && agents.length > 0 && (
          <div className="filter-bar" style={{ marginTop: "1rem", marginBottom: "1rem" }}>
            <label>
              Filter by Agent:{" "}
              <select
                value={selectedAgent}
                onChange={(e) =>
                  setSelectedAgent(e.target.value ? Number(e.target.value) : "")
                }
              >
                <option value="">All agents</option>
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.display_name} ({a.agent_key}@{a.agent_definition_version})
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}

        {showCreate && (
          <form className="form-card" onSubmit={handleCreate} style={{ marginTop: "1rem" }}>
            <h3>Add a new memory fact</h3>
            <label>
              Scope
              <select
                value={createScope}
                onChange={(e) => setCreateScope(e.target.value as "user" | "agent")}
              >
                <option value="user">User Profile (all agents)</option>
                <option value="agent">Agent Private (single agent)</option>
              </select>
            </label>

            {createScope === "agent" && (
              <label>
                Target Agent
                <select
                  value={createAgentId}
                  onChange={(e) => setCreateAgentId(Number(e.target.value))}
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
            )}

            <label>
              Fact content
              <textarea
                value={createContent}
                onChange={(e) => setCreateContent(e.target.value)}
                placeholder="e.g. User prefers Python and SQLite for local projects."
                required
                rows={3}
              />
            </label>

            <div className="form-actions" style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
              <button type="submit" disabled={actions.createMemory.isPending}>
                {actions.createMemory.isPending ? "Saving..." : "Save fact"}
              </button>
              <button type="button" onClick={() => setShowCreate(false)}>
                Cancel
              </button>
            </div>
            {actions.createMemory.isError && <InlineError error={actions.createMemory.error} />}
          </form>
        )}

        {memories.length === 0 ? (
          <p className="empty-state" style={{ marginTop: "1rem" }}>
            No active memory items found.
          </p>
        ) : (
          <div className="table-responsive" style={{ marginTop: "1rem" }}>
            <table className="data-table" style={{ width: "100%", textAlign: "left" }}>
              <thead>
                <tr>
                  <th>Scope</th>
                  <th>Target Agent</th>
                  <th>Content</th>
                  <th>Version</th>
                  <th>Provenance</th>
                  <th>Updated</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {memories.map((mem) => {
                  const agent = agents.find((a) => a.id === mem.agent_instance_id);
                  return (
                    <tr key={mem.id}>
                      <td>
                        <span className="badge">{mem.scope.toUpperCase()}</span>
                      </td>
                      <td>
                        {mem.scope === "agent"
                          ? agent?.display_name ?? `Agent #${mem.agent_instance_id}`
                          : "—"}
                      </td>
                      <td style={{ maxWidth: "300px", wordBreak: "break-word" }}>
                        {mem.content}
                      </td>
                      <td>v{mem.current_version}</td>
                      <td>
                        <span className="badge">
                          {mem.provenance_type === "user_authored"
                            ? "User authored"
                            : "Inferred"}
                        </span>
                      </td>
                      <td>{new Date(mem.updated_at).toLocaleString()}</td>
                      <td>
                        <div style={{ display: "flex", gap: "0.5rem" }}>
                          <button
                            type="button"
                            className="button-link"
                            onClick={() => setViewingHistoryId(mem.id)}
                          >
                            History
                          </button>
                          <button
                            type="button"
                            className="button-link"
                            onClick={() => handleStartEdit(mem)}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className="button-link text-danger"
                            onClick={() => setDeletingMemory(mem)}
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Edit Modal */}
      {editingMemory && (
        <div className="modal-backdrop" style={{ marginTop: "1.5rem" }}>
          <section className="panel form-card">
            <h3>Edit memory fact (v{editingMemory.current_version})</h3>
            <form onSubmit={handleSaveEdit}>
              <label>
                Content
                <textarea
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  rows={4}
                  required
                />
              </label>
              {editError && <p className="form-error text-danger">{editError}</p>}
              <div className="form-actions" style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
                <button type="submit" disabled={actions.editMemory.isPending}>
                  {actions.editMemory.isPending ? "Saving..." : "Save update"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditingMemory(null);
                    setEditError(null);
                  }}
                >
                  Cancel
                </button>
              </div>
            </form>
          </section>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deletingMemory && (
        <div className="modal-backdrop" style={{ marginTop: "1.5rem" }}>
          <section className="panel form-card">
            <h3>Confirm memory deletion</h3>
            <p>
              Are you sure you want to delete this memory? This removes the fact from active
              context and future runs.
            </p>
            <blockquote style={{ fontStyle: "italic", margin: "0.5rem 0" }}>
              "{deletingMemory.content}"
            </blockquote>
            <p className="text-secondary" style={{ fontSize: "0.875rem" }}>
              Note: Historical run snapshots that already used this memory will retain what they
              previously used.
            </p>
            <div className="form-actions" style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
              <button
                type="button"
                className="button-danger"
                onClick={handleDelete}
                disabled={actions.deleteMemory.isPending}
              >
                {actions.deleteMemory.isPending ? "Deleting..." : "Confirm delete"}
              </button>
              <button type="button" onClick={() => setDeletingMemory(null)}>
                Cancel
              </button>
            </div>
          </section>
        </div>
      )}

      {/* Version History Drawer/Modal */}
      {viewingHistoryId !== null && (
        <div className="modal-backdrop" style={{ marginTop: "1.5rem" }}>
          <section className="panel page-card">
            <div className="section-heading">
              <h3>Version History for Memory #{viewingHistoryId}</h3>
              <button
                type="button"
                className="button-link"
                onClick={() => setViewingHistoryId(null)}
              >
                Close
              </button>
            </div>

            {historyQ.isPending ? (
              <LoadingState message="Loading version history..." />
            ) : historyQ.isError ? (
              <InlineError error={historyQ.error} />
            ) : historyQ.data.items.length === 0 ? (
              <p>No historical versions found.</p>
            ) : (
              <table className="data-table" style={{ width: "100%", textAlign: "left", marginTop: "1rem" }}>
                <thead>
                  <tr>
                    <th>Version</th>
                    <th>Content</th>
                    <th>Source</th>
                    <th>Provenance</th>
                    <th>Created At</th>
                  </tr>
                </thead>
                <tbody>
                  {historyQ.data.items.map((ver: MemoryVersionItem) => (
                    <tr key={ver.id}>
                      <td>v{ver.version}</td>
                      <td style={{ maxWidth: "300px", wordBreak: "break-word" }}>{ver.content}</td>
                      <td>{ver.source_kind}</td>
                      <td>{ver.provenance_type}</td>
                      <td>{new Date(ver.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}
    </main>
  );
}
