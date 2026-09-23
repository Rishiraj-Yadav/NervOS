"""Owner-scoped Conversation routes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status
from nervos_core.application.conversations import (
    Conversation,
    ConversationTurnDetail,
)

from nervos_api.api.dependencies import (
    ConversationServiceDependency,
    CurrentUserDependency,
    OriginDependency,
)
from nervos_api.api.schemas import (
    ConversationCreateRequest,
    ConversationListItemResponse,
    ConversationMessageResponse,
    ConversationPageResponse,
    ConversationResponse,
    ConversationTurnPageResponse,
    ConversationTurnResponse,
    SendMessageRequest,
)

router = APIRouter(prefix="/conversations")

PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]
BeforeSequence = Annotated[int | None, Query(gt=0)]


def _conversation_response(conv: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=conv.id,
        owner_user_id=conv.owner_user_id,
        agent_instance_id=conv.agent_instance_id,
        title=conv.title,
        status=conv.status.value,
        archived_at=conv.archived_at,
        deleted_at=conv.deleted_at,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


def _page_conversations(items: list[Conversation], limit: int) -> ConversationPageResponse:
    return ConversationPageResponse(
        items=[
            ConversationListItemResponse(
                id=c.id,
                owner_user_id=c.owner_user_id,
                agent_instance_id=c.agent_instance_id,
                title=c.title,
                status=c.status.value,
                archived_at=c.archived_at,
                deleted_at=c.deleted_at,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c in items
        ],
        next_before_id=items[-1].id if len(items) == limit else None,
    )


def _turn_response(detail: ConversationTurnDetail) -> ConversationTurnResponse:
    return ConversationTurnResponse(
        id=detail.turn.id,
        conversation_id=detail.turn.conversation_id,
        sequence=detail.turn.sequence,
        state=detail.turn.state.value,
        client_message_id=detail.turn.client_message_id,
        authoritative_run_id=detail.turn.authoritative_run_id,
        created_at=detail.turn.created_at,
        started_at=detail.turn.started_at,
        finished_at=detail.turn.finished_at,
        user_message=ConversationMessageResponse(
            id=detail.user_message.id,
            turn_id=detail.user_message.turn_id,
            role=detail.user_message.role.value,
            content=detail.user_message.content,
            source_run_id=detail.user_message.source_run_id,
            created_at=detail.user_message.created_at,
        ),
        assistant_message=(
            ConversationMessageResponse(
                id=detail.assistant_message.id,
                turn_id=detail.assistant_message.turn_id,
                role=detail.assistant_message.role.value,
                content=detail.assistant_message.content,
                source_run_id=detail.assistant_message.source_run_id,
                created_at=detail.assistant_message.created_at,
            )
            if detail.assistant_message is not None
            else None
        ),
        latest_run_id=detail.latest_run_id,
        latest_run_status=detail.latest_run_status,
        is_retryable=detail.is_retryable,
    )


def _page_turns(turns: list[ConversationTurnDetail], limit: int) -> ConversationTurnPageResponse:
    return ConversationTurnPageResponse(
        items=[_turn_response(t) for t in turns],
        next_before_sequence=turns[-1].turn.sequence if len(turns) == limit else None,
    )


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    body: ConversationCreateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
) -> ConversationResponse:
    """Explicitly create one empty conversation belonging to an owned Agent Instance."""
    del origin
    conversation = service.create_conversation(
        owner_user_id=user.id,
        agent_instance_id=body.agent_instance_id,
        title=body.title,
    )
    return _conversation_response(conversation)


@router.get("", response_model=ConversationPageResponse)
def list_conversations(
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
    status: Literal["active", "archived"] = "active",
    limit: PageLimit = 20,
    before_id: BeforeId = None,
    agent_instance_id: int | None = None,
) -> ConversationPageResponse:
    """Return one newest-first page of conversations owned by the authenticated user."""
    conversations = service.list_conversations(
        user.id,
        limit=limit,
        before_id=before_id,
        agent_instance_id=agent_instance_id,
        status=status,
    )
    return _page_conversations(list(conversations), limit)


@router.get("/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: int,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
) -> ConversationResponse:
    """Return one owned conversation; foreign and nonexistent ids are indistinguishable."""
    return _conversation_response(service.get_conversation(user.id, conversation_id))


@router.post(
    "/{conversation_id}/archive",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
)
def archive_conversation(
    conversation_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
) -> ConversationResponse:
    """Archive an active conversation."""
    del origin
    conv = service.archive_conversation(user.id, conversation_id)
    return _conversation_response(conv)


@router.post(
    "/{conversation_id}/unarchive",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
)
def unarchive_conversation(
    conversation_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
) -> ConversationResponse:
    """Restore an archived conversation back to active."""
    del origin
    conv = service.unarchive_conversation(user.id, conversation_id)
    return _conversation_response(conv)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_conversation(
    conversation_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
) -> Response:
    """Soft delete a conversation."""
    del origin
    service.delete_conversation(user.id, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{conversation_id}/messages",
    response_model=ConversationTurnResponse,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    conversation_id: int,
    body: SendMessageRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
    response: Response,
) -> ConversationTurnResponse:
    """Submit a USER message to an existing conversation and begin turn execution."""
    del origin
    turn_detail, is_duplicate = service.send_message(
        owner_user_id=user.id,
        conversation_id=conversation_id,
        client_message_id=body.client_message_id,
        content=body.content,
    )
    if is_duplicate:
        response.status_code = status.HTTP_200_OK
    return _turn_response(turn_detail)


@router.get(
    "/{conversation_id}/turns",
    response_model=ConversationTurnPageResponse,
)
def list_turns(
    conversation_id: int,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
    limit: PageLimit = 20,
    before_sequence: BeforeSequence = None,
) -> ConversationTurnPageResponse:
    """Return one newest-first page of turns in the conversation."""
    turns = service.list_turns(
        user.id,
        conversation_id=conversation_id,
        limit=limit,
        before_sequence=before_sequence,
    )
    return _page_turns(list(turns), limit)


@router.post(
    "/{conversation_id}/turns/{turn_id}/retry",
    response_model=ConversationTurnResponse,
    status_code=status.HTTP_200_OK,
)
def retry_turn(
    conversation_id: int,
    turn_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ConversationServiceDependency,
) -> ConversationTurnResponse:
    """Retry execution for the latest failed or cancelled turn."""
    del origin
    turn_detail, _ = service.retry_turn(
        owner_user_id=user.id,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
    return _turn_response(turn_detail)
