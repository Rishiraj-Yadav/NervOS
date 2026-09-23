"""ContextBuilder application service: deterministic bounded context assembly."""

from __future__ import annotations

from datetime import datetime

from nervos_core.application.memory import RetrievedMemoryItem
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.context import (
    AGENT_MEMORY_HEADER,
    COMPACTION_HEADER_STORED,
    CONTEXT_BUILDER_VERSION_V2,
    CONTEXT_SCHEMA_VERSION_V2,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_CODE_POINTS,
    MAX_INJECTED_MEMORY_BYTES,
    MAX_RECENT_HISTORY_MESSAGES,
    USER_MEMORY_HEADER,
    CompactionData,
    ContextSnapshotData,
    HistoricalMessage,
    SelectedMemory,
    compute_context_digest,
    render_context_v2,
)
from nervos_core.domain.conversations import (
    ConversationMessage,
    MessageRole,
)
from nervos_core.domain.memory import MemoryScope


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
        memory_candidates: tuple[RetrievedMemoryItem, ...] = (),
        max_bytes: int = MAX_CONTEXT_BYTES,
        max_code_points: int = MAX_CONTEXT_CODE_POINTS,
    ) -> ContextSnapshotData:
        """Assemble bounded multi-turn context deterministically."""
        user_bytes = len(current_user_text.encode("utf-8"))
        user_cp = len(current_user_text)
        if user_bytes > max_bytes or user_cp > max_code_points:
            raise ValueError("current_user_text exceeds total context budget")

        # 1. Select approved USER and AGENT memories (indivisible items, up to 4000 bytes)
        rem_bytes = max_bytes - user_bytes
        avail_mem_bytes = min(MAX_INJECTED_MEMORY_BYTES, rem_bytes)

        user_mem_candidates = [m for m in memory_candidates if m.scope is MemoryScope.USER]
        agent_mem_candidates = [m for m in memory_candidates if m.scope is MemoryScope.AGENT]

        selected_user_memories: list[RetrievedMemoryItem] = []
        selected_agent_memories: list[RetrievedMemoryItem] = []

        # Evaluate USER memories
        for item in user_mem_candidates:
            test_selected = [*selected_user_memories, item]
            test_user_text = (
                USER_MEMORY_HEADER + "\n" + "\n".join(f"- {m.content}" for m in test_selected)
            )
            test_render = render_context_v2(
                current_user_text=current_user_text,
                injected_user_memory_text=test_user_text,
            )
            render_bytes = len(test_render.encode("utf-8"))
            render_cp = len(test_render)
            mem_bytes = len(test_user_text.encode("utf-8"))
            if (
                render_bytes <= max_bytes
                and render_cp <= max_code_points
                and mem_bytes <= avail_mem_bytes
            ):
                selected_user_memories.append(item)

        injected_user_text = (
            USER_MEMORY_HEADER + "\n" + "\n".join(f"- {m.content}" for m in selected_user_memories)
            if selected_user_memories
            else None
        )

        # Evaluate AGENT memories
        for item in agent_mem_candidates:
            test_selected = [*selected_agent_memories, item]
            test_agent_text = (
                AGENT_MEMORY_HEADER + "\n" + "\n".join(f"- {m.content}" for m in test_selected)
            )
            test_render = render_context_v2(
                current_user_text=current_user_text,
                injected_user_memory_text=injected_user_text,
                injected_agent_memory_text=test_agent_text,
            )
            render_bytes = len(test_render.encode("utf-8"))
            render_cp = len(test_render)
            total_mem_bytes = (
                len(injected_user_text.encode("utf-8")) + 2 if injected_user_text else 0
            ) + len(test_agent_text.encode("utf-8"))
            if (
                render_bytes <= max_bytes
                and render_cp <= max_code_points
                and total_mem_bytes <= avail_mem_bytes
            ):
                selected_agent_memories.append(item)

        injected_agent_text = (
            AGENT_MEMORY_HEADER
            + "\n"
            + "\n".join(f"- {m.content}" for m in selected_agent_memories)
            if selected_agent_memories
            else None
        )

        # 2. Select contiguous recent history suffix (reverse chronological scan, up to 10 pairs)
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
            test_rendered = render_context_v2(
                current_user_text=current_user_text,
                injected_user_memory_text=injected_user_text,
                injected_agent_memory_text=injected_agent_text,
                injected_compaction_text=None,
                history_messages=tuple(candidate_messages),
            )
            if (
                len(test_rendered.encode("utf-8")) <= max_bytes
                and len(test_rendered) <= max_code_points
            ):
                selected_turns.insert(0, (seq, u_msg, a_msg))
                selected_messages = candidate_messages
            else:
                break

        # 3. Evaluate Compaction validity & trimming
        injected_compaction: str | None = None
        compaction_version: int | None = None
        compaction_start: int | None = None
        compaction_end: int | None = None

        if current_compaction is not None and candidate_turns:
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
                stored_body = current_compaction.content
                if stored_body.startswith(COMPACTION_HEADER_STORED + "\n"):
                    stored_body = stored_body[len(COMPACTION_HEADER_STORED) + 1 :]
                elif stored_body.startswith(COMPACTION_HEADER_STORED):
                    stored_body = stored_body[len(COMPACTION_HEADER_STORED) :].lstrip("\n")

                test_render = render_context_v2(
                    current_user_text=current_user_text,
                    injected_user_memory_text=injected_user_text,
                    injected_agent_memory_text=injected_agent_text,
                    injected_compaction_text=stored_body,
                    history_messages=tuple(selected_messages),
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
                    turn_blocks = [b.strip() for b in stored_body.split("\n\n") if b.strip()]
                    fitted_blocks: list[str] = []
                    for block in reversed(turn_blocks):
                        candidate_blocks = [block, *fitted_blocks]
                        candidate_text = "\n\n".join(candidate_blocks)
                        test_render = render_context_v2(
                            current_user_text=current_user_text,
                            injected_user_memory_text=injected_user_text,
                            injected_agent_memory_text=injected_agent_text,
                            injected_compaction_text=candidate_text,
                            history_messages=tuple(selected_messages),
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

        # 4. Final Canonical Render & Digest
        final_rendered = render_context_v2(
            current_user_text=current_user_text,
            injected_user_memory_text=injected_user_text,
            injected_agent_memory_text=injected_agent_text,
            injected_compaction_text=injected_compaction,
            history_messages=tuple(selected_messages),
        )
        actual_bytes = len(final_rendered.encode("utf-8"))
        actual_cp = len(final_rendered)
        digest = compute_context_digest(final_rendered)

        selected_turn_ids = tuple(t[1].turn_id for t in selected_turns)
        selected_message_ids = tuple(m.message_id for m in selected_messages)

        selected_memories = tuple(
            SelectedMemory(
                item_id=m.item_id,
                version=m.version,
                scope=m.scope.value,
                provenance_type=m.provenance_type.value,
                content=m.content,
                content_digest=m.content_digest,
            )
            for m in (selected_user_memories + selected_agent_memories)
        )

        return ContextSnapshotData(
            run_id=0,
            turn_id=0,
            schema_version=CONTEXT_SCHEMA_VERSION_V2,
            builder_version=CONTEXT_BUILDER_VERSION_V2,
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
            selected_memories=selected_memories,
            injected_user_memory_text=injected_user_text,
            injected_agent_memory_text=injected_agent_text,
        )


__all__ = ["ContextBuilder"]
