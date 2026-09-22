"""ContextBuilder application service: deterministic bounded context assembly."""

from __future__ import annotations

from datetime import datetime

from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.context import (
    COMPACTION_HEADER_STORED,
    CONTEXT_BUILDER_VERSION,
    CONTEXT_SCHEMA_VERSION,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_CODE_POINTS,
    MAX_RECENT_HISTORY_MESSAGES,
    CompactionData,
    ContextSnapshotData,
    HistoricalMessage,
    compute_context_digest,
    render_context_v1,
)
from nervos_core.domain.conversations import (
    ConversationMessage,
    MessageRole,
)


class ContextBuilder:
    """Deterministic, provider-neutral conversational context assembly service."""

    @staticmethod
    def assemble(
        *,
        current_user_text: str,
        candidate_turns: list[tuple[int, ConversationMessage, ConversationMessage]],
        current_compaction: CompactionData | None,
        agent_definition_id: AgentDefinitionId,
        now: datetime,
        max_bytes: int = MAX_CONTEXT_BYTES,
        max_code_points: int = MAX_CONTEXT_CODE_POINTS,
    ) -> ContextSnapshotData:
        """Assemble bounded multi-turn context deterministically.

        candidate_turns: list of (turn_sequence, user_message, assistant_message) for
        committed SUCCEEDED turns with sequence < current_sequence, in chronological order.
        """
        user_bytes = len(current_user_text.encode("utf-8"))
        user_cp = len(current_user_text)
        if user_bytes > max_bytes or user_cp > max_code_points:
            raise ValueError("current_user_text exceeds total context budget")

        # 1. Select contiguous recent history suffix (reverse chronological scan, up to 10 pairs)
        max_pairs = MAX_RECENT_HISTORY_MESSAGES // 2
        recent_candidates = (
            candidate_turns[-max_pairs:] if len(candidate_turns) > max_pairs else candidate_turns
        )

        selected_turns: list[tuple[int, ConversationMessage, ConversationMessage]] = []
        selected_messages: list[HistoricalMessage] = []

        for seq, u_msg, a_msg in reversed(recent_candidates):
            test_pair_messages = [
                HistoricalMessage(
                    turn_id=u_msg.turn_id,
                    sequence=seq,
                    message_id=u_msg.id,
                    role=MessageRole.USER,
                    content=u_msg.content,
                ),
                HistoricalMessage(
                    turn_id=a_msg.turn_id,
                    sequence=seq,
                    message_id=a_msg.id,
                    role=MessageRole.ASSISTANT,
                    content=a_msg.content,
                ),
            ]
            candidate_messages = test_pair_messages + selected_messages
            test_rendered = render_context_v1(
                current_user_text=current_user_text,
                history_messages=tuple(candidate_messages),
                injected_compaction_text=None,
            )
            if (
                len(test_rendered.encode("utf-8")) <= max_bytes
                and len(test_rendered) <= max_code_points
            ):
                selected_turns.insert(0, (seq, u_msg, a_msg))
                selected_messages = candidate_messages
            else:
                # Stop immediately upon hitting first pair that doesn't fit (contiguous suffix rule)
                break

        # 2. Evaluate Compaction validity & trimming
        injected_compaction: str | None = None
        compaction_version: int | None = None
        compaction_start: int | None = None
        compaction_end: int | None = None

        if current_compaction is not None and candidate_turns:
            # Determine expected compaction cutoff
            if selected_turns:
                oldest_selected_seq = selected_turns[0][0]
                older_candidates = [t[0] for t in candidate_turns if t[0] < oldest_selected_seq]
                expected_compaction_end = max(older_candidates) if older_candidates else None
            else:
                expected_compaction_end = max(t[0] for t in candidate_turns)

            if (
                expected_compaction_end is not None
                and current_compaction.source_end_sequence == expected_compaction_end
                and current_compaction.source_start_sequence
                <= current_compaction.source_end_sequence
            ):
                # Compaction is valid, exact, and non-overlapping
                stored_body = current_compaction.content
                if stored_body.startswith(COMPACTION_HEADER_STORED + "\n"):
                    stored_body = stored_body[len(COMPACTION_HEADER_STORED) + 1 :]
                elif stored_body.startswith(COMPACTION_HEADER_STORED):
                    stored_body = stored_body[len(COMPACTION_HEADER_STORED) :].lstrip("\n")

                # Check if full body fits
                test_render = render_context_v1(
                    current_user_text=current_user_text,
                    history_messages=tuple(selected_messages),
                    injected_compaction_text=stored_body,
                )
                if (
                    len(test_render.encode("utf-8")) <= max_bytes
                    and len(test_render) <= max_code_points
                ):
                    injected_compaction = stored_body
                    compaction_version = current_compaction.version
                    compaction_start = current_compaction.source_start_sequence
                    compaction_end = current_compaction.source_end_sequence
                else:
                    # Trim complete turn blocks (retaining newest blocks closest to recent history)
                    turn_blocks = [b.strip() for b in stored_body.split("\n\n") if b.strip()]
                    fitted_blocks: list[str] = []
                    for block in reversed(turn_blocks):
                        candidate_blocks = [block, *fitted_blocks]
                        candidate_text = "\n\n".join(candidate_blocks)
                        test_render = render_context_v1(
                            current_user_text=current_user_text,
                            history_messages=tuple(selected_messages),
                            injected_compaction_text=candidate_text,
                        )
                        if (
                            len(test_render.encode("utf-8")) <= max_bytes
                            and len(test_render) <= max_code_points
                        ):
                            fitted_blocks = candidate_blocks
                        else:
                            break

                    if fitted_blocks:
                        injected_compaction = "\n\n".join(fitted_blocks)
                        compaction_version = current_compaction.version
                        compaction_start = current_compaction.source_start_sequence
                        compaction_end = current_compaction.source_end_sequence

        # 3. Final Canonical Render & Digest
        final_rendered = render_context_v1(
            current_user_text=current_user_text,
            history_messages=tuple(selected_messages),
            injected_compaction_text=injected_compaction,
        )
        actual_bytes = len(final_rendered.encode("utf-8"))
        actual_cp = len(final_rendered)
        digest = compute_context_digest(final_rendered)

        selected_turn_ids = tuple(t[1].turn_id for t in selected_turns)
        selected_message_ids = tuple(m.message_id for m in selected_messages)

        return ContextSnapshotData(
            run_id=0,  # Assigned when Run is inserted
            turn_id=0,  # Assigned when Turn is inserted
            schema_version=CONTEXT_SCHEMA_VERSION,
            builder_version=CONTEXT_BUILDER_VERSION,
            current_user_text=current_user_text,
            history_messages=tuple(selected_messages),
            selected_turn_ids=selected_turn_ids,
            selected_message_ids=selected_message_ids,
            compaction_version=compaction_version,
            compaction_source_start=compaction_start,
            compaction_source_end=compaction_end,
            injected_compaction_text=injected_compaction,
            agent_key=agent_definition_id.agent_key,
            agent_definition_version=agent_definition_id.agent_definition_version,
            max_total_bytes=max_bytes,
            max_total_code_points=max_code_points,
            actual_total_bytes=actual_bytes,
            actual_total_code_points=actual_cp,
            rendered_context=final_rendered,
            content_digest=digest,
            created_at=now,
        )


__all__ = ["ContextBuilder"]
