import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  conversationDetailQuery,
  conversationTurnsQuery,
  useConversationActions,
} from "../api/conversationQueries";
import { ErrorState, LoadingState } from "../components/AsyncState";

export function ConversationPage() {
  const navigate = useNavigate();
  const { conversationId } = useParams<{ conversationId: string }>();
  const id = Number(conversationId);
  const actions = useConversationActions();

  const [messageText, setMessageText] = useState("");
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const convQ = useQuery(conversationDetailQuery(id));
  const turnsQ = useQuery(conversationTurnsQuery(id, 2000));

  if (convQ.isPending || turnsQ.isPending) return <LoadingState />;
  if (convQ.isError)
    return <ErrorState error={convQ.error} onRetry={() => void convQ.refetch()} />;
  if (turnsQ.isError)
    return <ErrorState error={turnsQ.error} onRetry={() => void turnsQ.refetch()} />;

  const conv = convQ.data;
  const turns = turnsQ.data.items;
  const isArchived = conv.status === "archived";

  // Chronological order for display
  const chronologicalTurns = [...turns].sort((a, b) => a.sequence - b.sequence);

  const handleSend = (e: React.FormEvent) => {
    e.preventDefault();
    if (!messageText.trim() || isArchived) return;
    const clientMessageId = crypto.randomUUID();
    actions.sendMessage.mutate(
      {
        conversationId: id,
        body: {
          client_message_id: clientMessageId,
          content: messageText.trim(),
        },
      },
      {
        onSuccess: () => {
          setMessageText("");
        },
      }
    );
  };

  const handleDelete = () => {
    actions.deleteConversation.mutate(id, {
      onSuccess: () => {
        navigate("/conversations");
      },
    });
  };

  const hasRunningTurn = turns.some(
    (t) => t.state === "pending" || t.state === "running"
  );

  return (
    <main className="page-shell">
      <header
        className="page-header"
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
        }}
      >
        <div>
          <p className="eyebrow">
            <Link to="/conversations">← All conversations</Link>
          </p>
          <h1>{conv.title || "New conversation"}</h1>
          <p>
            Agent {conv.agent_instance_id} · Created{" "}
            {new Date(conv.created_at).toLocaleString()}
            {conv.archived_at && ` · Archived ${new Date(conv.archived_at).toLocaleString()}`}
          </p>
        </div>

        <div style={{ display: "flex", gap: "0.5rem" }}>
          {isArchived ? (
            <button
              type="button"
              className="button-link"
              onClick={() => actions.unarchiveConversation.mutate(id)}
              disabled={actions.unarchiveConversation.isPending}
            >
              Unarchive
            </button>
          ) : (
            <button
              type="button"
              className="button-link"
              onClick={() => actions.archiveConversation.mutate(id)}
              disabled={actions.archiveConversation.isPending}
            >
              Archive
            </button>
          )}
          <button
            type="button"
            className="button-link text-danger"
            onClick={() => setShowDeleteConfirm(true)}
          >
            Delete
          </button>
        </div>
      </header>

      {isArchived && (
        <div
          className="panel"
          style={{
            marginBottom: "1rem",
            backgroundColor: "var(--bg-card-muted, #f8fafc)",
            borderLeft: "4px solid var(--warning, #f59e0b)",
            padding: "0.75rem 1rem",
          }}
        >
          <p style={{ margin: 0, fontWeight: 500 }}>
            This conversation is archived. Unarchive it to send new messages.
          </p>
        </div>
      )}

      <section className="panel page-card">
        <div className="section-heading">
          <h2>Messages</h2>
        </div>

        {chronologicalTurns.length === 0 ? (
          <p>No messages yet. Send a message to start the conversation.</p>
        ) : (
          <div className="stack conversation-thread">
            {chronologicalTurns.map((turn) => (
              <div key={turn.id} className="turn-card" style={{ marginBottom: "1.5rem" }}>
                {/* User Message */}
                <div
                  className="message user-message"
                  style={{
                    backgroundColor: "var(--accent-light, #e0f2fe)",
                    padding: "0.75rem 1rem",
                    borderRadius: "8px",
                    marginBottom: "0.5rem",
                  }}
                >
                  <p className="eyebrow" style={{ margin: "0 0 0.25rem 0", fontSize: "0.75rem" }}>
                    User · Turn {turn.sequence}
                  </p>
                  <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{turn.user_message.content}</p>
                </div>

                {/* Assistant Message or Pending/Error state */}
                {turn.assistant_message ? (
                  <div
                    className="message assistant-message"
                    style={{
                      backgroundColor: "var(--card-bg, #f1f5f9)",
                      padding: "0.75rem 1rem",
                      borderRadius: "8px",
                    }}
                  >
                    <p className="eyebrow" style={{ margin: "0 0 0.25rem 0", fontSize: "0.75rem" }}>
                      Assistant
                    </p>
                    <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                      {turn.assistant_message.content}
                    </p>
                  </div>
                ) : turn.state === "pending" || turn.state === "running" ? (
                  <div className="status-inline" style={{ padding: "0.5rem" }}>
                    <span className="spinner" aria-hidden="true" />
                    <span>Thinking...</span>
                  </div>
                ) : turn.state === "failed" ? (
                  <div
                    className="status-error"
                    style={{ color: "var(--danger, #ef4444)", padding: "0.5rem 0" }}
                  >
                    <p style={{ margin: "0 0 0.5rem 0" }}>Execution failed for this turn.</p>
                    {turn.is_retryable && !isArchived && (
                      <button
                        type="button"
                        onClick={() =>
                          actions.retryTurn.mutate({
                            conversationId: id,
                            turnId: turn.id,
                          })
                        }
                      >
                        {actions.retryTurn.isPending ? "Retrying..." : "Retry turn"}
                      </button>
                    )}
                  </div>
                ) : turn.state === "cancelled" ? (
                  <div
                    className="status-error"
                    style={{ color: "var(--muted, #64748b)", padding: "0.5rem 0" }}
                  >
                    <p style={{ margin: "0 0 0.5rem 0" }}>Turn execution was cancelled.</p>
                    {turn.is_retryable && !isArchived && (
                      <button
                        type="button"
                        onClick={() =>
                          actions.retryTurn.mutate({
                            conversationId: id,
                            turnId: turn.id,
                          })
                        }
                      >
                        {actions.retryTurn.isPending ? "Retrying..." : "Retry turn"}
                      </button>
                    )}
                  </div>
                ) : turn.state === "ambiguous" ? (
                  <p style={{ color: "var(--warning, #f59e0b)" }}>
                    Execution status was ambiguous; please send a new message.
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        )}

        <form
          className="form-card"
          onSubmit={handleSend}
          style={{ marginTop: "2rem" }}
        >
          <label>
            Your message
            <textarea
              rows={3}
              value={messageText}
              onChange={(e) => setMessageText(e.target.value)}
              placeholder={isArchived ? "Conversation is archived" : "Type your message..."}
              disabled={actions.sendMessage.isPending || hasRunningTurn || isArchived}
              required
            />
          </label>

          <button
            type="submit"
            disabled={
              actions.sendMessage.isPending ||
              hasRunningTurn ||
              !messageText.trim() ||
              isArchived
            }
          >
            {actions.sendMessage.isPending
              ? "Sending..."
              : hasRunningTurn
              ? "Turn in progress..."
              : "Send"}
          </button>
        </form>
      </section>

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="modal-backdrop" style={{ marginTop: "1.5rem" }}>
          <section className="panel form-card">
            <h3>Delete this conversation?</h3>
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
                onClick={handleDelete}
                disabled={actions.deleteConversation.isPending}
              >
                {actions.deleteConversation.isPending ? "Deleting..." : "Confirm delete"}
              </button>
              <button type="button" onClick={() => setShowDeleteConfirm(false)}>
                Cancel
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
