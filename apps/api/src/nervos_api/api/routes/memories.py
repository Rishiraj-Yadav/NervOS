"""Memory creation, inspection, promotion, editing, and deletion endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from nervos_api.api.dependencies import (
    CurrentUserDependency,
    MemoryServiceDependency,
    OriginDependency,
)
from nervos_api.api.schemas import (
    MemoryCreateRequest,
    MemoryEditRequest,
    MemoryItemResponse,
    MemoryPageResponse,
    MemoryPromoteRequest,
    MemoryVersionPageResponse,
    MemoryVersionResponse,
)

router = APIRouter(tags=["memories"])

PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]
BeforeVersion = Annotated[int | None, Query(gt=0)]


@router.get(
    "",
    response_model=MemoryPageResponse,
    status_code=status.HTTP_200_OK,
)
def list_memories(
    current_user: CurrentUserDependency,
    memory_service: MemoryServiceDependency,
    scope: Literal["user", "agent"] | None = Query(None),
    agent_instance_id: int | None = Query(None, gt=0),
    limit: PageLimit = 20,
    before_id: BeforeId = None,
) -> MemoryPageResponse:
    """List active memories owned by the current user."""
    items, next_before_id = memory_service.list_memories(
        owner_user_id=current_user.id,
        scope=scope,
        agent_instance_id=agent_instance_id,
        before_id=before_id,
        limit=limit,
    )
    return MemoryPageResponse(
        items=[MemoryItemResponse.from_domain(item) for item in items],
        next_before_id=next_before_id,
    )


@router.get(
    "/{memory_id}",
    response_model=MemoryItemResponse,
    status_code=status.HTTP_200_OK,
)
def get_memory(
    memory_id: int,
    current_user: CurrentUserDependency,
    memory_service: MemoryServiceDependency,
) -> MemoryItemResponse:
    """Get active memory item detail by ID."""
    detail = memory_service.get_memory(
        owner_user_id=current_user.id,
        memory_item_id=memory_id,
    )
    return MemoryItemResponse.from_domain(detail)


@router.get(
    "/{memory_id}/versions",
    response_model=MemoryVersionPageResponse,
    status_code=status.HTTP_200_OK,
)
def list_memory_versions(
    memory_id: int,
    current_user: CurrentUserDependency,
    memory_service: MemoryServiceDependency,
    limit: PageLimit = 20,
    before_version: BeforeVersion = None,
) -> MemoryVersionPageResponse:
    """List historical versions of an active memory item."""
    versions, next_before_version = memory_service.list_memory_versions(
        owner_user_id=current_user.id,
        memory_item_id=memory_id,
        before_version=before_version,
        limit=limit,
    )
    return MemoryVersionPageResponse(
        items=[MemoryVersionResponse.from_domain(v) for v in versions],
        next_before_version=next_before_version,
    )


@router.patch(
    "/{memory_id}",
    response_model=MemoryItemResponse,
    status_code=status.HTTP_200_OK,
)
def edit_memory(
    memory_id: int,
    payload: MemoryEditRequest,
    current_user: CurrentUserDependency,
    _: OriginDependency,
    memory_service: MemoryServiceDependency,
) -> MemoryItemResponse:
    """Edit active memory content using expected_version optimistic concurrency."""
    detail = memory_service.edit_memory(
        owner_user_id=current_user.id,
        memory_item_id=memory_id,
        expected_version=payload.expected_version,
        content=payload.content,
    )
    return MemoryItemResponse.from_domain(detail)


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_memory(
    memory_id: int,
    current_user: CurrentUserDependency,
    _: OriginDependency,
    memory_service: MemoryServiceDependency,
    expected_version: int | None = Query(None, ge=1),
) -> Response:
    """Soft delete an active memory item."""
    memory_service.delete_memory(
        owner_user_id=current_user.id,
        memory_item_id=memory_id,
        expected_version=expected_version,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "",
    response_model=MemoryItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_memory(
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
def promote_memory(
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
