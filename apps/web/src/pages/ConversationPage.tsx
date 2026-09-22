import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  conversationDetailQuery,
  conversationTurnsQuery,
  useConversationActions,
} from "../api/conversationQueries";
import { ErrorState, LoadingState } from "../components/AsyncState";

export function ConversationPage() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const id = Number(conversationId);
  const actions = useConversationActions();

  const [messageText, setMessageText] = useState("");

  const convQ = useQuery(conversationDetailQuery(id));
  const turnsQ = useQuery(conversationTurnsQuery(id, 2000));

  if (convQ.isPending || turnsQ.isPending) return <LoadingState />;
  if (convQ.isError)
    return <ErrorState error={convQ.error} onRetry={() => void convQ.refetch()} />;
  if (turnsQ.isError)
    return <ErrorState error={turnsQ.error} onRetry={() => void turnsQ.refetch()} />;

  const conv = convQ.data;
  const turns = turnsQ.data.items;

  // Chronological order for display
  const chronologicalTurns = [...turns].sort((a, b) => a.sequence - b.sequence);

  const handleSend = (e: React.FormEvent) => {
    e.preventDefault();
    if (!messageText.trim()) return;
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

  const hasRunningTurn = turns.some(
    (t) => t.state === "pending" || t.state === "running"
  );

  return (
    <main className="page-shell">
      <header className="page-header">
        <div>
          <p className="eyebrow">
            <Link to="/conversations">← All conversations</Link>
          </p>
          <h1>{conv.title || "New conversation"}</h1>
          <p>
            Agent {conv.agent_instance_id} · Created{" "}
            {new Date(conv.created_at).toLocaleString()}
          </p>
        </div>
      </header>

      <section className="panel page-card">
        <div className="section-heading">
          <h2>Messages</h2>
        </div>

        {chronologicalTurns.length === 0 ? (
          <p>No messages yet. Send a message to start the conversation.</p>
        ) : (
          <div className="stack" style={{ gap: "1.5rem" }}>
            {chronologicalTurns.map((turn) => (
              <div
                key={turn.id}
                className="turn-container"
                style={{
                  padding: "1rem",
                  border: "1px solid var(--border-color, #e2e8f0)",
                  borderRadius: "8px",
                }}
              >
                <div style={{ marginBottom: "0.5rem" }}>
                  <small style={{ color: "var(--muted, #64748b)" }}>
                    Turn #{turn.sequence} ·{" "}
                    <span
                      style={{
                        fontWeight: "bold",
                        textTransform: "capitalize",
                      }}
                    >
                      {turn.state}
                    </span>
                    {turn.latest_run_id && (
                      <span> · Run #{turn.latest_run_id}</span>
                    )}
                  </small>
                </div>

                <div
                  className="user-message"
                  style={{
                    backgroundColor: "var(--user-msg-bg, #f1f5f9)",
                    padding: "0.75rem",
                    borderRadius: "6px",
                    marginBottom: "0.75rem",
                  }}
                >
                  <strong>User:</strong>
                  <p style={{ margin: "0.25rem 0 0 0", whiteSpace: "pre-wrap" }}>
                    {turn.user_message.content}
                  </p>
                </div>

                {turn.assistant_message ? (
                  <div
                    className="assistant-message"
                    style={{
                      backgroundColor: "var(--assistant-msg-bg, #e0f2fe)",
                      padding: "0.75rem",
                      borderRadius: "6px",
                    }}
                  >
                    <strong>Assistant:</strong>
                    <p style={{ margin: "0.25rem 0 0 0", whiteSpace: "pre-wrap" }}>
                      {turn.assistant_message.content}
                    </p>
                  </div>
                ) : turn.state === "running" || turn.state === "pending" ? (
                  <p style={{ fontStyle: "italic", color: "var(--muted, #64748b)" }}>
                    Assistant is thinking...
                  </p>
                ) : turn.state === "failed" ? (
                  <div style={{ color: "var(--danger, #ef4444)" }}>
                    <p style={{ margin: "0 0 0.5rem 0" }}>
                      Execution failed for this turn.
                    </p>
                    {turn.is_retryable && (
                      <button
                        className="secondary"
                        disabled={actions.retryTurn.isPending}
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
                  <div style={{ color: "var(--muted, #64748b)" }}>
                    <p style={{ margin: "0 0 0.5rem 0" }}>
                      Execution was cancelled.
                    </p>
                    {turn.is_retryable && (
                      <button
                        className="secondary"
                        disabled={actions.retryTurn.isPending}
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
              placeholder="Type your message..."
              disabled={actions.sendMessage.isPending || hasRunningTurn}
              required
            />
          </label>

          <button
            type="submit"
            disabled={
              actions.sendMessage.isPending ||
              hasRunningTurn ||
              !messageText.trim()
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
    </main>
  );
}
