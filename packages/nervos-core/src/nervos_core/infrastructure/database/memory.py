"""Owner-scoped Memory and MemoryVersion persistence."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.engine import Connection, RowMapping

from nervos_core.application.memory import (
    MemoryRetrievalQuery,
    RetrievedMemoryItem,
)
from nervos_core.domain.memory import (
    InvalidMemorySource,
    MemoryItem,
    MemoryItemDetail,
    MemoryNotFound,
    MemoryProvenanceType,
    MemoryScope,
    MemoryScopeCapacityExceeded,
    MemorySourceKind,
    MemoryStatus,
    MemoryVersion,
    StaleMemoryVersion,
    compute_memory_digest,
    validate_memory_content,
)
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    ConversationMessageRecord,
    ConversationRecord,
    ConversationTurnRecord,
    MemoryItemRecord,
    MemoryVersionRecord,
    RunRecord,
)
from nervos_core.infrastructure.database.transaction import TransactionRunner


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_memory_item(record: MemoryItemRecord | RowMapping) -> MemoryItem:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    updated = _as_utc(record["updated_at"] if isinstance(record, RowMapping) else record.updated_at)
    assert created is not None
    assert updated is not None
    agent_id = (
        record["agent_instance_id"] if isinstance(record, RowMapping) else record.agent_instance_id
    )
    return MemoryItem(
        id=int(record["id"] if isinstance(record, RowMapping) else record.id),
        owner_user_id=int(
            record["owner_user_id"] if isinstance(record, RowMapping) else record.owner_user_id
        ),
        agent_instance_id=int(agent_id) if agent_id is not None else None,
        scope=MemoryScope(str(record["scope"] if isinstance(record, RowMapping) else record.scope)),
        status=MemoryStatus(
            str(record["status"] if isinstance(record, RowMapping) else record.status)
        ),
        current_version=int(
            record["current_version"] if isinstance(record, RowMapping) else record.current_version
        ),
        created_at=created,
        updated_at=updated,
    )


def _to_memory_version(record: MemoryVersionRecord | RowMapping) -> MemoryVersion:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    assert created is not None
    src_id = record["source_id"] if isinstance(record, RowMapping) else record.source_id
    return MemoryVersion(
        id=int(record["id"] if isinstance(record, RowMapping) else record.id),
        memory_item_id=int(
            record["memory_item_id"] if isinstance(record, RowMapping) else record.memory_item_id
        ),
        version=int(record["version"] if isinstance(record, RowMapping) else record.version),
        content=str(record["content"] if isinstance(record, RowMapping) else record.content),
        content_digest=bytes(
            record["content_digest"] if isinstance(record, RowMapping) else record.content_digest
        ),
        source_kind=MemorySourceKind(
            str(record["source_kind"] if isinstance(record, RowMapping) else record.source_kind)
        ),
        source_id=int(src_id) if src_id is not None else None,
        provenance_type=MemoryProvenanceType(
            str(
                record["provenance_type"]
                if isinstance(record, RowMapping)
                else record.provenance_type
            )
        ),
        created_by_user_id=int(
            record["created_by_user_id"]
            if isinstance(record, RowMapping)
            else record.created_by_user_id
        ),
        created_at=created,
    )


def _retrieve_memory_candidates_on_connection(
    connection: Connection,
    owner_user_id: int,
    agent_instance_id: int,
    candidate_limit: int = 50,
) -> tuple[RetrievedMemoryItem, ...]:
    """Retrieve up to 50 active memory candidates using keyset reservation and refill."""
    target_per_scope = 25

    # 1. Query active USER memory candidates (up to 25, newest first)
    user_rows = (
        connection.execute(
            select(
                MemoryItemRecord.id.label("item_id"),
                MemoryItemRecord.scope,
                MemoryVersionRecord.version,
                MemoryVersionRecord.content,
                MemoryVersionRecord.content_digest,
                MemoryVersionRecord.provenance_type,
                MemoryVersionRecord.created_at,
            )
            .join(
                MemoryVersionRecord,
                and_(
                    MemoryVersionRecord.memory_item_id == MemoryItemRecord.id,
                    MemoryVersionRecord.version == MemoryItemRecord.current_version,
                ),
            )
            .where(
                MemoryItemRecord.owner_user_id == owner_user_id,
                MemoryItemRecord.scope == MemoryScope.USER.value,
                MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
            )
            .order_by(MemoryItemRecord.id.desc())
            .limit(target_per_scope)
        )
        .mappings()
        .all()
    )

    # 2. Query active AGENT memory candidates (up to 25, newest first)
    agent_rows = (
        connection.execute(
            select(
                MemoryItemRecord.id.label("item_id"),
                MemoryItemRecord.scope,
                MemoryVersionRecord.version,
                MemoryVersionRecord.content,
                MemoryVersionRecord.content_digest,
                MemoryVersionRecord.provenance_type,
                MemoryVersionRecord.created_at,
            )
            .join(
                MemoryVersionRecord,
                and_(
                    MemoryVersionRecord.memory_item_id == MemoryItemRecord.id,
                    MemoryVersionRecord.version == MemoryItemRecord.current_version,
                ),
            )
            .where(
                MemoryItemRecord.owner_user_id == owner_user_id,
                MemoryItemRecord.agent_instance_id == agent_instance_id,
                MemoryItemRecord.scope == MemoryScope.AGENT.value,
                MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
            )
            .order_by(MemoryItemRecord.id.desc())
            .limit(target_per_scope)
        )
        .mappings()
        .all()
    )

    user_candidates = list(user_rows)
    agent_candidates = list(agent_rows)

    # 3. Keyset Refill if one scope has fewer than 25 items
    if len(user_candidates) < target_per_scope and len(agent_candidates) == target_per_scope:
        refill_needed = target_per_scope - len(user_candidates)
        last_agent_id = int(agent_candidates[-1]["item_id"])
        additional_agent_rows = (
            connection.execute(
                select(
                    MemoryItemRecord.id.label("item_id"),
                    MemoryItemRecord.scope,
                    MemoryVersionRecord.version,
                    MemoryVersionRecord.content,
                    MemoryVersionRecord.content_digest,
                    MemoryVersionRecord.provenance_type,
                    MemoryVersionRecord.created_at,
                )
                .join(
                    MemoryVersionRecord,
                    and_(
                        MemoryVersionRecord.memory_item_id == MemoryItemRecord.id,
                        MemoryVersionRecord.version == MemoryItemRecord.current_version,
                    ),
                )
                .where(
                    MemoryItemRecord.owner_user_id == owner_user_id,
                    MemoryItemRecord.agent_instance_id == agent_instance_id,
                    MemoryItemRecord.scope == MemoryScope.AGENT.value,
                    MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    MemoryItemRecord.id < last_agent_id,
                )
                .order_by(MemoryItemRecord.id.desc())
                .limit(refill_needed)
            )
            .mappings()
            .all()
        )
        agent_candidates.extend(additional_agent_rows)

    elif len(agent_candidates) < target_per_scope and len(user_candidates) == target_per_scope:
        refill_needed = target_per_scope - len(agent_candidates)
        last_user_id = int(user_candidates[-1]["item_id"])
        additional_user_rows = (
            connection.execute(
                select(
                    MemoryItemRecord.id.label("item_id"),
                    MemoryItemRecord.scope,
                    MemoryVersionRecord.version,
                    MemoryVersionRecord.content,
                    MemoryVersionRecord.content_digest,
                    MemoryVersionRecord.provenance_type,
                    MemoryVersionRecord.created_at,
                )
                .join(
                    MemoryVersionRecord,
                    and_(
                        MemoryVersionRecord.memory_item_id == MemoryItemRecord.id,
                        MemoryVersionRecord.version == MemoryItemRecord.current_version,
                    ),
                )
                .where(
                    MemoryItemRecord.owner_user_id == owner_user_id,
                    MemoryItemRecord.scope == MemoryScope.USER.value,
                    MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    MemoryItemRecord.id < last_user_id,
                )
                .order_by(MemoryItemRecord.id.desc())
                .limit(refill_needed)
            )
            .mappings()
            .all()
        )
        user_candidates.extend(additional_user_rows)

    # 4. Total Ordering: Scope order (USER=0, AGENT=1), then item_id DESC
    combined = user_candidates + agent_candidates

    items: list[RetrievedMemoryItem] = []
    for row in combined[:candidate_limit]:
        created = _as_utc(row["created_at"])
        assert created is not None
        items.append(
            RetrievedMemoryItem(
                item_id=int(row["item_id"]),
                version=int(row["version"]),
                scope=MemoryScope(str(row["scope"])),
                provenance_type=MemoryProvenanceType(str(row["provenance_type"])),
                content=str(row["content"]),
                content_digest=bytes(row["content_digest"]),
                created_at=created,
            )
        )
    return tuple(items)


class SqlAlchemyMemoryPersistence:
    """SQLAlchemy implementation of Memory and MemoryVersion persistence."""

    def __init__(
        self,
        engine: Engine,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._engine = engine
        self._runner = TransactionRunner(engine, sleep=sleep if sleep is not None else time.sleep)

    def create_memory(
        self,
        *,
        owner_user_id: int,
        scope: MemoryScope,
        content: str,
        content_digest: bytes,
        agent_instance_id: int | None,
        source_kind: MemorySourceKind,
        source_id: int | None,
        provenance_type: MemoryProvenanceType,
        now: datetime,
    ) -> MemoryItemDetail:
        def operation(connection: Connection) -> MemoryItemDetail:
            # 1. Enforce active items cap (<= 1000 per scope)
            if scope is MemoryScope.USER:
                count = connection.scalar(
                    select(func.count())
                    .select_from(MemoryItemRecord)
                    .where(
                        MemoryItemRecord.owner_user_id == owner_user_id,
                        MemoryItemRecord.scope == MemoryScope.USER.value,
                        MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    )
                )
            else:
                count = connection.scalar(
                    select(func.count())
                    .select_from(MemoryItemRecord)
                    .where(
                        MemoryItemRecord.owner_user_id == owner_user_id,
                        MemoryItemRecord.agent_instance_id == agent_instance_id,
                        MemoryItemRecord.scope == MemoryScope.AGENT.value,
                        MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    )
                )

            if int(count or 0) >= 1000:
                raise MemoryScopeCapacityExceeded(
                    f"Memory scope {scope.value} capacity exceeded (maximum 1000 active items)"
                )

            # 2. Insert MemoryItemRecord (active, current_version=1)
            item_stmt = (
                insert(MemoryItemRecord)
                .values(
                    owner_user_id=owner_user_id,
                    agent_instance_id=agent_instance_id,
                    scope=scope.value,
                    status=MemoryStatus.ACTIVE.value,
                    current_version=1,
                    created_at=now,
                    updated_at=now,
                )
                .returning(MemoryItemRecord)
            )
            item_row = connection.execute(item_stmt).mappings().one()
            item = _to_memory_item(item_row)

            # 3. Insert MemoryVersionRecord (version=1)
            ver_stmt = (
                insert(MemoryVersionRecord)
                .values(
                    memory_item_id=item.id,
                    version=1,
                    content=content,
                    content_digest=content_digest,
                    source_kind=source_kind.value,
                    source_id=source_id,
                    provenance_type=provenance_type.value,
                    created_by_user_id=owner_user_id,
                    created_at=now,
                )
                .returning(MemoryVersionRecord)
            )
            ver_row = connection.execute(ver_stmt).mappings().one()
            version = _to_memory_version(ver_row)

            return MemoryItemDetail(item=item, current_version_record=version)

        return self._runner.run(operation)

    def promote_memory(
        self,
        *,
        owner_user_id: int,
        source_type: str,
        source_id: int,
        scope: MemoryScope,
        content: str | None,
        agent_instance_id: int | None,
        now: datetime,
    ) -> MemoryItemDetail:
        def operation(connection: Connection) -> MemoryItemDetail:
            # 1. Resolve and validate source
            if source_type == "conversation_message":
                msg_row = (
                    connection.execute(
                        select(
                            ConversationMessageRecord.content,
                            ConversationRecord.owner_user_id,
                            ConversationRecord.agent_instance_id,
                        )
                        .join(
                            ConversationTurnRecord,
                            ConversationTurnRecord.id == ConversationMessageRecord.turn_id,
                        )
                        .join(
                            ConversationRecord,
                            ConversationRecord.id == ConversationTurnRecord.conversation_id,
                        )
                        .where(ConversationMessageRecord.id == source_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if msg_row is None or int(msg_row["owner_user_id"]) != owner_user_id:
                    raise InvalidMemorySource(
                        f"conversation message {source_id} not found or not owned"
                    )
                if (
                    scope is MemoryScope.AGENT
                    and int(msg_row["agent_instance_id"]) != agent_instance_id
                ):
                    raise InvalidMemorySource(
                        f"conversation message {source_id} does not belong to agent"
                    )

                source_kind = MemorySourceKind.PROMOTED_MESSAGE
                raw_text = content if content is not None else str(msg_row["content"])

            elif source_type == "run":
                run_row = (
                    connection.execute(
                        select(
                            RunRecord.status,
                            RunRecord.output_text,
                            AgentInstanceRecord.owner_user_id,
                            RunRecord.agent_instance_id,
                        )
                        .join(
                            AgentInstanceRecord,
                            AgentInstanceRecord.id == RunRecord.agent_instance_id,
                        )
                        .where(RunRecord.id == source_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if run_row is None or int(run_row["owner_user_id"]) != owner_user_id:
                    raise InvalidMemorySource(f"run {source_id} not found or not owned")
                if (
                    scope is MemoryScope.AGENT
                    and int(run_row["agent_instance_id"]) != agent_instance_id
                ):
                    raise InvalidMemorySource(
                        f"run {source_id} does not belong to agent instance {agent_instance_id}"
                    )
                if run_row["status"] != RunStatus.SUCCEEDED.value or run_row["output_text"] is None:
                    raise InvalidMemorySource(
                        "Only succeeded runs with output text can be promoted to memory"
                    )

                source_kind = MemorySourceKind.PROMOTED_RUN
                raw_text = content if content is not None else str(run_row["output_text"])

            else:
                raise InvalidMemorySource(f"unsupported source_type: {source_type}")

            valid_content = validate_memory_content(raw_text)
            digest = compute_memory_digest(valid_content)

            # 2. Check active items cap (<= 1000 per scope)
            if scope is MemoryScope.USER:
                count = connection.scalar(
                    select(func.count())
                    .select_from(MemoryItemRecord)
                    .where(
                        MemoryItemRecord.owner_user_id == owner_user_id,
                        MemoryItemRecord.scope == MemoryScope.USER.value,
                        MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    )
                )
            else:
                count = connection.scalar(
                    select(func.count())
                    .select_from(MemoryItemRecord)
                    .where(
                        MemoryItemRecord.owner_user_id == owner_user_id,
                        MemoryItemRecord.agent_instance_id == agent_instance_id,
                        MemoryItemRecord.scope == MemoryScope.AGENT.value,
                        MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    )
                )

            if int(count or 0) >= 1000:
                raise MemoryScopeCapacityExceeded(
                    f"Memory scope {scope.value} capacity exceeded (maximum 1000 active items)"
                )

            # 3. Insert MemoryItemRecord & MemoryVersionRecord
            item_stmt = (
                insert(MemoryItemRecord)
                .values(
                    owner_user_id=owner_user_id,
                    agent_instance_id=agent_instance_id,
                    scope=scope.value,
                    status=MemoryStatus.ACTIVE.value,
                    current_version=1,
                    created_at=now,
                    updated_at=now,
                )
                .returning(MemoryItemRecord)
            )
            item_row = connection.execute(item_stmt).mappings().one()
            item = _to_memory_item(item_row)

            ver_stmt = (
                insert(MemoryVersionRecord)
                .values(
                    memory_item_id=item.id,
                    version=1,
                    content=valid_content,
                    content_digest=digest,
                    source_kind=source_kind.value,
                    source_id=source_id,
                    provenance_type=MemoryProvenanceType.USER_APPROVED_INFERRED.value,
                    created_by_user_id=owner_user_id,
                    created_at=now,
                )
                .returning(MemoryVersionRecord)
            )
            ver_row = connection.execute(ver_stmt).mappings().one()
            version = _to_memory_version(ver_row)

            return MemoryItemDetail(item=item, current_version_record=version)

        return self._runner.run(operation)

    def get_memory(self, owner_user_id: int, memory_item_id: int) -> MemoryItemDetail:
        def operation(connection: Connection) -> MemoryItemDetail:
            row = (
                connection.execute(
                    select(
                        MemoryItemRecord.id,
                        MemoryItemRecord.owner_user_id,
                        MemoryItemRecord.agent_instance_id,
                        MemoryItemRecord.scope,
                        MemoryItemRecord.status,
                        MemoryItemRecord.current_version,
                        MemoryItemRecord.created_at,
                        MemoryItemRecord.updated_at,
                        MemoryVersionRecord.id.label("version_id"),
                        MemoryVersionRecord.version,
                        MemoryVersionRecord.content,
                        MemoryVersionRecord.content_digest,
                        MemoryVersionRecord.source_kind,
                        MemoryVersionRecord.source_id,
                        MemoryVersionRecord.provenance_type,
                        MemoryVersionRecord.created_by_user_id,
                        MemoryVersionRecord.created_at.label("version_created_at"),
                    )
                    .join(
                        MemoryVersionRecord,
                        and_(
                            MemoryVersionRecord.memory_item_id == MemoryItemRecord.id,
                            MemoryVersionRecord.version == MemoryItemRecord.current_version,
                        ),
                    )
                    .where(
                        MemoryItemRecord.id == memory_item_id,
                        MemoryItemRecord.owner_user_id == owner_user_id,
                        MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise MemoryNotFound(f"Memory {memory_item_id} not found")

            item = _to_memory_item(row)
            version = MemoryVersion(
                id=int(row["version_id"]),
                memory_item_id=item.id,
                version=int(row["version"]),
                content=str(row["content"]),
                content_digest=bytes(row["content_digest"]),
                source_kind=MemorySourceKind(str(row["source_kind"])),
                source_id=int(row["source_id"]) if row["source_id"] is not None else None,
                provenance_type=MemoryProvenanceType(str(row["provenance_type"])),
                created_by_user_id=int(row["created_by_user_id"]),
                created_at=_as_utc(row["version_created_at"]) or item.created_at,
            )
            return MemoryItemDetail(item=item, current_version_record=version)

        return self._runner.run(operation)

    def list_memories(
        self,
        *,
        owner_user_id: int,
        scope: MemoryScope | None = None,
        agent_instance_id: int | None = None,
        before_id: int | None = None,
        limit: int = 20,
    ) -> tuple[tuple[MemoryItemDetail, ...], int | None]:
        def operation(connection: Connection) -> tuple[tuple[MemoryItemDetail, ...], int | None]:
            stmt = (
                select(
                    MemoryItemRecord.id,
                    MemoryItemRecord.owner_user_id,
                    MemoryItemRecord.agent_instance_id,
                    MemoryItemRecord.scope,
                    MemoryItemRecord.status,
                    MemoryItemRecord.current_version,
                    MemoryItemRecord.created_at,
                    MemoryItemRecord.updated_at,
                    MemoryVersionRecord.id.label("version_id"),
                    MemoryVersionRecord.version,
                    MemoryVersionRecord.content,
                    MemoryVersionRecord.content_digest,
                    MemoryVersionRecord.source_kind,
                    MemoryVersionRecord.source_id,
                    MemoryVersionRecord.provenance_type,
                    MemoryVersionRecord.created_by_user_id,
                    MemoryVersionRecord.created_at.label("version_created_at"),
                )
                .join(
                    MemoryVersionRecord,
                    and_(
                        MemoryVersionRecord.memory_item_id == MemoryItemRecord.id,
                        MemoryVersionRecord.version == MemoryItemRecord.current_version,
                    ),
                )
                .where(
                    MemoryItemRecord.owner_user_id == owner_user_id,
                    MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                )
            )
            if scope is not None:
                stmt = stmt.where(MemoryItemRecord.scope == scope.value)
            if agent_instance_id is not None:
                stmt = stmt.where(MemoryItemRecord.agent_instance_id == agent_instance_id)
            if before_id is not None:
                stmt = stmt.where(MemoryItemRecord.id < before_id)

            stmt = stmt.order_by(MemoryItemRecord.id.desc()).limit(limit)
            rows = connection.execute(stmt).mappings().all()

            items = tuple(
                MemoryItemDetail(
                    item=_to_memory_item(row),
                    current_version_record=MemoryVersion(
                        id=int(row["version_id"]),
                        memory_item_id=int(row["id"]),
                        version=int(row["version"]),
                        content=str(row["content"]),
                        content_digest=bytes(row["content_digest"]),
                        source_kind=MemorySourceKind(str(row["source_kind"])),
                        source_id=int(row["source_id"]) if row["source_id"] is not None else None,
                        provenance_type=MemoryProvenanceType(str(row["provenance_type"])),
                        created_by_user_id=int(row["created_by_user_id"]),
                        created_at=_as_utc(row["version_created_at"])
                        or _to_memory_item(row).created_at,
                    ),
                )
                for row in rows
            )
            next_before_id = items[-1].item.id if len(items) == limit else None
            return items, next_before_id

        return self._runner.run(operation)

    def list_memory_versions(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        before_version: int | None = None,
        limit: int = 20,
    ) -> tuple[tuple[MemoryVersion, ...], int | None]:
        def operation(connection: Connection) -> tuple[tuple[MemoryVersion, ...], int | None]:
            # Verify owner owns the item
            item_exists = connection.execute(
                select(MemoryItemRecord.id).where(
                    MemoryItemRecord.id == memory_item_id,
                    MemoryItemRecord.owner_user_id == owner_user_id,
                    MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if item_exists is None:
                raise MemoryNotFound(f"Memory {memory_item_id} not found")

            stmt = select(MemoryVersionRecord).where(
                MemoryVersionRecord.memory_item_id == memory_item_id,
            )
            if before_version is not None:
                stmt = stmt.where(MemoryVersionRecord.version < before_version)

            stmt = stmt.order_by(MemoryVersionRecord.version.desc()).limit(limit)
            rows = connection.execute(stmt).mappings().all()

            versions = tuple(_to_memory_version(row) for row in rows)
            next_before_ver = versions[-1].version if len(versions) == limit else None
            return versions, next_before_ver

        return self._runner.run(operation)

    def edit_memory(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        expected_version: int,
        content: str,
        content_digest: bytes,
        now: datetime,
    ) -> MemoryItemDetail:
        def operation(connection: Connection) -> MemoryItemDetail:
            item_row = (
                connection.execute(
                    select(MemoryItemRecord).where(
                        MemoryItemRecord.id == memory_item_id,
                        MemoryItemRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if item_row is None or str(item_row["status"]) != MemoryStatus.ACTIVE.value:
                raise MemoryNotFound(f"Memory {memory_item_id} not found")

            current_ver = int(item_row["current_version"])
            if current_ver != expected_version:
                raise StaleMemoryVersion(
                    f"Memory item {memory_item_id} version mismatch:"
                    f" expected {expected_version}, got {current_ver}"
                )

            next_ver = current_ver + 1

            # Insert new version record
            ver_stmt = (
                insert(MemoryVersionRecord)
                .values(
                    memory_item_id=memory_item_id,
                    version=next_ver,
                    content=content,
                    content_digest=content_digest,
                    source_kind=MemorySourceKind.DIRECT_USER.value,
                    source_id=None,
                    provenance_type=MemoryProvenanceType.USER_AUTHORED.value,
                    created_by_user_id=owner_user_id,
                    created_at=now,
                )
                .returning(MemoryVersionRecord)
            )
            ver_row = connection.execute(ver_stmt).mappings().one()
            new_version = _to_memory_version(ver_row)

            # Atomic CAS update of MemoryItemRecord
            update_stmt = (
                update(MemoryItemRecord)
                .where(
                    MemoryItemRecord.id == memory_item_id,
                    MemoryItemRecord.owner_user_id == owner_user_id,
                    MemoryItemRecord.current_version == expected_version,
                    MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
                )
                .values(
                    current_version=next_ver,
                    updated_at=now,
                )
                .returning(MemoryItemRecord)
            )
            updated_item_row = connection.execute(update_stmt).mappings().one_or_none()
            if updated_item_row is None:
                raise StaleMemoryVersion(
                    f"Memory item {memory_item_id} version was concurrently modified"
                )

            item = _to_memory_item(updated_item_row)
            return MemoryItemDetail(item=item, current_version_record=new_version)

        return self._runner.run(operation)

    def delete_memory(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        expected_version: int | None = None,
        now: datetime,
    ) -> None:
        def operation(connection: Connection) -> None:
            item_row = (
                connection.execute(
                    select(MemoryItemRecord).where(
                        MemoryItemRecord.id == memory_item_id,
                        MemoryItemRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if item_row is None:
                raise MemoryNotFound(f"Memory {memory_item_id} not found")

            if str(item_row["status"]) == MemoryStatus.DELETED.value:
                # Idempotent success
                return

            current_ver = int(item_row["current_version"])
            if expected_version is not None and current_ver != expected_version:
                raise StaleMemoryVersion(
                    f"Memory item {memory_item_id} version mismatch:"
                    f" expected {expected_version}, got {current_ver}"
                )

            update_stmt = update(MemoryItemRecord).where(
                MemoryItemRecord.id == memory_item_id,
                MemoryItemRecord.owner_user_id == owner_user_id,
                MemoryItemRecord.status == MemoryStatus.ACTIVE.value,
            )
            if expected_version is not None:
                update_stmt = update_stmt.where(
                    MemoryItemRecord.current_version == expected_version
                )
            update_stmt = update_stmt.values(
                status=MemoryStatus.DELETED.value,
                updated_at=now,
            )
            res = connection.execute(update_stmt)
            if expected_version is not None and res.rowcount != 1:
                raise StaleMemoryVersion(
                    f"Memory item {memory_item_id} version was concurrently modified"
                )

        return self._runner.run(operation)

    def retrieve_candidates(self, query: MemoryRetrievalQuery) -> tuple[RetrievedMemoryItem, ...]:
        def operation(connection: Connection) -> tuple[RetrievedMemoryItem, ...]:
            return _retrieve_memory_candidates_on_connection(
                connection,
                owner_user_id=query.owner_user_id,
                agent_instance_id=query.agent_instance_id,
                candidate_limit=query.candidate_limit,
            )

        return self._runner.run(operation)


__all__ = [
    "SqlAlchemyMemoryPersistence",
    "_retrieve_memory_candidates_on_connection",
]
