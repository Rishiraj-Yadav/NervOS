"""Tool permission evaluation and grant management.

D2 implements the fail-closed permission layer over the D1 schema.

Grant operations are owner-scoped and transactionally checked. Permission decisions
are evaluated fresh at call time from durable grant rows. No annotation or metadata
ever grants authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Protocol


class GrantAlreadyExists(ValueError):
    """Attempted to grant a tool that already has a current grant."""


class ToolDefinitionNotFound(LookupError):
    """Tool definition does not exist or is not accessible to this owner."""


class McpConnectionNotFound(LookupError):
    """MCP connection does not exist or is not accessible to this owner."""


class OwnershipMismatch(ValueError):
    """Attempted to grant a tool from a connection not owned by the Agent's owner."""


class PermissionDenialReason(Enum):
    """Typed denial reasons for permission decisions.

    These are internal classification only. They are never exposed to the model
    and never become string-based security logic.
    """

    # No grant row exists for this (agent, tool) pair
    NOT_GRANTED = auto()

    # Grant exists but its id > run.tool_grant_cutoff_id
    GRANT_AFTER_RUN_CUTOFF = auto()

    # Grant's reviewed_fingerprint != current definition fingerprint
    DEFINITION_CHANGED = auto()

    # Tool definition is marked unavailable or needs review
    DEFINITION_UNAVAILABLE = auto()

    # MCP connection is disabled
    CONNECTION_DISABLED = auto()

    # Cross-owner attempt (Agent's owner != connection's owner)
    OWNER_MISMATCH = auto()

    # Hard runtime policy denial (future use)
    HARD_POLICY_DENIED = auto()


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """Result of a call-time permission check.

    Either allowed=True with no reason, or allowed=False with a typed reason.
    Never expose detailed internal denial reasons to the model.
    """

    allowed: bool
    reason: PermissionDenialReason | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.reason is not None:
            raise ValueError("allowed decision cannot have a denial reason")
        if not self.allowed and self.reason is None:
            raise ValueError("denied decision must have a reason")


@dataclass(frozen=True, slots=True)
class ToolGrant:
    """A durable ALLOW grant binding one Agent Instance to one Tool Definition.

    The row's existence is the authority. There is no DENY row.
    """

    id: int  # AUTOINCREMENT, the monotonic ordering primitive
    agent_instance_id: int
    tool_definition_id: int
    reviewed_fingerprint: str  # Immutable for this row's lifetime
    created_at: datetime


class ToolPermissionPersistence(Protocol):
    """Durable grant operations.

    All operations are owner-scoped and transactionally checked.
    """

    def grant_tool(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
        now: datetime,
    ) -> ToolGrant:
        """Create an ALLOW grant for (agent, tool).

        Checks:
        - Agent Instance exists and belongs to owner
        - Tool Definition exists
        - For MCP tools: connection exists, enabled, and belongs to same owner
        - For builtin tools: no ownership check on definition

        Uses the definition's CURRENT fingerprint as reviewed_fingerprint.

        Idempotent: if a grant already exists with the SAME current fingerprint,
        returns the existing grant. If fingerprint differs (definition drifted),
        raises GrantAlreadyExists — caller must revoke first.

        Raises:
            ToolDefinitionNotFound: definition missing or inaccessible
            McpConnectionNotFound: MCP connection missing or inaccessible
            OwnershipMismatch: MCP connection belongs to different owner
            GrantAlreadyExists: grant exists with different fingerprint
        """
        ...

    def revoke_tool(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
    ) -> None:
        """Delete the grant row for (agent, tool).

        Owner-scoped. Idempotent: if no grant exists, returns successfully.
        Revocation takes effect immediately at the next call-time check.

        Raises:
            Nothing on missing grant (idempotent)
        """
        ...

    def reconfirm_tool(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
        now: datetime,
    ) -> ToolGrant:
        """Reconfirm a drifted definition by replacing the grant.

        Atomically: delete old grant → insert new grant with new AUTOINCREMENT id
        and current fingerprint.

        The new id is strictly greater than any existing Run's cutoff, so the
        re-confirmed capability becomes visible only to new Runs.

        Raises:
            ToolDefinitionNotFound: if definition is missing
            Same ownership/availability checks as grant_tool
        """
        ...

    def list_grants(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
    ) -> tuple[ToolGrant, ...]:
        """List all current grants for an Agent Instance.

        Owner-scoped. Returns empty tuple if no grants exist.
        """
        ...

    def list_grants_at_or_before_cutoff(
        self,
        *,
        agent_instance_id: int,
        grant_cutoff_id: int,
    ) -> tuple[ToolGrant, ...]:
        """List the grant rows of one Agent Instance whose id is at or below a Run's cutoff.

        This is a **cutoff filter only**. It answers exactly one question:

            grant.agent_instance_id == agent_instance_id
            grant.id               <= grant_cutoff_id
            the grant row still exists

        It is deliberately NOT a permission decision and NOT a complete eligibility
        verdict. It does not check the definition's fingerprint, availability, the MCP
        connection's enabled state, or ownership. Those are live conditions the
        call-time evaluator decides, and catalog assembly must still route every
        candidate through it.

        Naming note: this was previously called ``list_eligible_grants``. "Eligible"
        overstated it -- eligibility in the full sense requires fingerprint match,
        definition availability, connection enabled state and owner agreement, none of
        which are evaluated here. The precise name is part of the API.

        The cutoff is a durable monotonic id, never a clock, so:

        - a grant added after the Run was submitted has a strictly greater id and is
          therefore invisible to it -- including to a retry Attempt;
        - revoking deletes the row, so an existing Run loses the authority at its very
          next check;
        - re-granting or re-confirming mints a new id above any existing cutoff, so the
          capability is visible only to Runs submitted afterwards.

        **Cutoff membership is not authority.** This returns what *may* be offered; the
        call-time evaluator still decides what may be *done*, from live rows.
        """
        ...


class ToolPermissionEvaluator(Protocol):
    """Call-time permission decision.

    Evaluates from live durable rows immediately before dispatch.
    Never mutates state. Never caches.
    """

    def check_permission(
        self,
        *,
        run_id: int,
        tool_definition_id: int,
    ) -> PermissionDecision:
        """Evaluate whether a tool call is allowed right now.

        Derives Agent Instance, grant cutoff, and definition fingerprint from
        durable rows, so caller cannot override these authority inputs.

        Checks (in fail-closed order):
        1. Run exists and belongs to exactly one Agent Instance
        2. Grant exists for (run.agent_instance_id, tool)
        3. Grant.id <= run.tool_grant_cutoff_id (from durable Run row)
        4. Grant.reviewed_fingerprint == definition.fingerprint (both from DB)
        5. Definition is available (not marked for review)
        6. For MCP: connection exists, enabled, same owner as Run's Agent
        7. Hard runtime policies (future)

        Returns PermissionDecision with typed reason on denial.

        This is a pure query — no rows are written, no state is mutated.
        """
        ...
