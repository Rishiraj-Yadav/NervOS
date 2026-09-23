"""Memory creation and promotion endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from nervos_api.api.dependencies import (
    CurrentUserDependency,
    MemoryServiceDependency,
    OriginDependency,
)
from nervos_api.api.schemas import (
    MemoryCreateRequest,
    MemoryItemResponse,
    MemoryPromoteRequest,
)

router = APIRouter(tags=["memories"])


@router.post(
    "",
    response_model=MemoryItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_memory(
    payload: MemoryCreateRequest,
    current_user: CurrentUserDependency,
    _: OriginDependency,
    memory_service: MemoryServiceDependency,
) -> MemoryItemResponse:
    """Create a direct user-authored memory item."""
    detail = memory_service.create_memory(
        owner_user_id=current_user.id,
        scope=payload.scope,
        content=payload.content,
        agent_instance_id=payload.agent_instance_id,
    )
    return MemoryItemResponse.from_domain(detail)


@router.post(
    "/promote",
    response_model=MemoryItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def promote_memory(
    payload: MemoryPromoteRequest,
    current_user: CurrentUserDependency,
    _: OriginDependency,
    memory_service: MemoryServiceDependency,
) -> MemoryItemResponse:
    """Explicitly promote a conversation message or succeeded run output to memory."""
    detail = memory_service.promote_memory(
        owner_user_id=current_user.id,
        source_type=payload.source_type,
        source_id=payload.source_id,
        scope=payload.scope,
        content=payload.content,
        agent_instance_id=payload.agent_instance_id,
    )
    return MemoryItemResponse.from_domain(detail)
