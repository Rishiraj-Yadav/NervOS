"""Owner-scoped MCP connection lifecycle for the control plane.

The control plane decides *which* tool source is approved, and asks what it currently offers. It
never speaks MCP itself: discovery enters through a port the composition root injects, so nothing in
this module -- or in the application service it calls -- imports the SDK or knows what a session is.

Refresh is synchronous and bounded. It is deliberately not a queued job: a durable job type for
discovery would be a second execution authority to reason about, and correctness here must not rest
on a background task outliving the request that asked for it. The response is returned once
reconciliation has committed, so an owner who sees `connected` is looking at durable truth.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from nervos_core.application.mcp_connections import (
    ConnectionTransport,
    DeleteOutcome,
    McpConnectionRow,
)
from nervos_core.application.tool_permissions import McpConnectionNotFound

from nervos_api.api.dependencies import (
    CurrentUserDependency,
    McpConnectionServiceDependency,
    OriginDependency,
)
from nervos_api.api.errors import McpConnectionHasHistory, McpConnectionHasLiveRun
from nervos_api.api.schemas import (
    McpConnectionCreateRequest,
    McpConnectionDeletedResponse,
    McpConnectionDisplayNameRequest,
    McpConnectionPageResponse,
    McpConnectionResponse,
)

router = APIRouter(prefix="/mcp-connections")

# Keyset pagination, shared with the accepted Agent Instance semantics.
PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]


def _page(connections: list[McpConnectionRow], limit: int) -> McpConnectionPageResponse:
    """Return one page plus the cursor for the next, when a further page may exist."""
    return McpConnectionPageResponse(
        items=[McpConnectionResponse.from_domain(connection) for connection in connections],
        next_before_id=connections[-1].connection_id if len(connections) == limit else None,
    )


@router.get("", response_model=McpConnectionPageResponse)
def list_connections(
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
    limit: PageLimit = 20,
    before_id: BeforeId = None,
) -> McpConnectionPageResponse:
    """Return one newest-first page of MCP connections owned by the authenticated user."""
    return _page(
        list(service.list_connections(user.id, limit=limit, before_id=before_id)), limit
    )


@router.post("", response_model=McpConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_connection(
    body: McpConnectionCreateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionResponse:
    """Record an approved tool source. Nothing is contacted and no tool is discovered yet.

    The row starts `needs_refresh`, so a connection that has never spoken to its server offers
    nothing until a refresh says otherwise.
    """
    del origin
    connection = service.create_connection(
        user.id,
        display_name=body.display_name,
        transport=ConnectionTransport(body.transport),
        endpoint=body.endpoint,
        server_key=body.server_key,
        credential_ref=body.credential_ref,
    )
    return McpConnectionResponse.from_domain(connection)


@router.get("/{connection_id}", response_model=McpConnectionResponse)
def get_connection(
    connection_id: int,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionResponse:
    """Return one owned connection. A foreign id and a missing one are the same 404."""
    return McpConnectionResponse.from_domain(service.get_connection(user.id, connection_id))


@router.post("/{connection_id}/refresh", response_model=McpConnectionResponse)
async def refresh_connection(
    connection_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionResponse:
    """Discover the connection's tools and reconcile them, synchronously and within one bound.

    The handler awaits the discovery rather than deferring it to a task: the request is the bound,
    and a caller who receives `connected` is looking at a committed verdict.
    """
    del origin
    return McpConnectionResponse.from_domain(await service.refresh(user.id, connection_id))


@router.post("/{connection_id}/enable", response_model=McpConnectionResponse)
def enable_connection(
    connection_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionResponse:
    """Re-enable a disabled connection, returning it to `needs_refresh`.

    Re-enabling is not a restore: the previous catalog verdict is discarded and the connection's
    definitions stay unavailable until a refresh proves them again. Otherwise flipping a flag back
    would make a stale catalog executable.
    """
    del origin
    return McpConnectionResponse.from_domain(service.enable_connection(user.id, connection_id))


@router.post("/{connection_id}/disable", response_model=McpConnectionResponse)
def disable_connection(
    connection_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionResponse:
    """Disable a connection. The next permission check denies it; no session is consulted."""
    del origin
    return McpConnectionResponse.from_domain(service.disable_connection(user.id, connection_id))


@router.patch("/{connection_id}", response_model=McpConnectionResponse)
def update_connection(
    connection_id: int,
    body: McpConnectionDisplayNameRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionResponse:
    """Change the connection's display name -- the only field a connection permits changing.

    Transport, endpoint, server key and credential alias are the connection's execution identity.
    Changing one would let a durable id silently point at different remote authority, so the service
    exposes no way to do it: a different target is a new connection.
    """
    del origin
    connection = service.update_display_name(user.id, connection_id, display_name=body.display_name)
    return McpConnectionResponse.from_domain(connection)


@router.delete("/{connection_id}", response_model=McpConnectionDeletedResponse)
def delete_connection(
    connection_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: McpConnectionServiceDependency,
) -> McpConnectionDeletedResponse:
    """Hard-delete a connection that has no audit history and no live Run that could reach it."""
    del origin
    outcome = service.delete_connection(user.id, connection_id)
    if outcome is DeleteOutcome.REFUSED_HAS_HISTORY:
        raise McpConnectionHasHistory
    if outcome is DeleteOutcome.REFUSED_LIVE_RUN:
        raise McpConnectionHasLiveRun
    if outcome is not DeleteOutcome.DELETED:  # pragma: no cover - NOT_FOUND raises in the service
        raise McpConnectionNotFound
    return McpConnectionDeletedResponse()
