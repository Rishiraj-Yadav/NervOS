"""Stage H3 durable per-action approvals (ADR 0034).

One approval row authorizes **at most one dispatch** of **one exact action input** on
**one Attempt**. The state machine is frozen by ADR 0034:

```text
pending → approved → consumed       (one dispatch)
pending → denied | expired | cancelled | revoked   (zero dispatches)
```

Consumption is a compare-and-set inside the mediator's `started` transaction: only an
`approved` row whose identity (Run, Attempt, sequence, definition, fingerprint, input
digest) matches the live call, whose `expires_at` has not passed, and whose owning grant
authority still exists transitions to `consumed`. Every other shape fails closed with zero
dispatches. The owner-facing decision API is idempotent per state transition.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nervos_core.application.clock import Clock
from nervos_core.application.tool_invocations import ClaimHandle, StartOutcome
from nervos_core.domain.tools import (
    JsonValue,
    ToolDescriptor,
    ToolSourceKind,
    canonical_json_text,
)

APPROVAL_STATES = ("pending", "approved", "consumed", "denied", "expired", "cancelled", "revoked")
DECIDABLE_STATES = ("pending",)
# Bounded expiry. The default is generous enough for a human to react; the bound keeps a
# forgotten approval from becoming a standing authority.
DEFAULT_EXPIRY_SECONDS = 15 * 60
MIN_EXPIRY_SECONDS = 60
MAX_EXPIRY_SECONDS = 24 * 60 * 60

_DIGEST_HEX = re.compile(r"[0-9a-f]{64}")


class ApprovalNotFound(LookupError):
    """The approval does not exist or belongs to another owner."""


class ApprovalConflict(ValueError):
    """The approval is not in a state that permits the requested transition."""


class InvalidApproval(ValueError):
    """An approval request violates the frozen policy."""


@dataclass(frozen=True, slots=True)
class ActionApproval:
    """Safe approval view for API/UI. `preview` is bounded, redacted content."""

    id: int
    owner_user_id: int
    agent_instance_id: int
    run_id: int
    job_id: int
    attempt_id: int
    tool_sequence: int
    tool_definition_id: int
    upstream_name: str
    fingerprint: str
    input_digest: str
    preview: Mapping[str, JsonValue]
    state: str
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Everything one durable approval binds, decided once, at creation.

    There is deliberately **no owner field**. The mediator knows the Run and the Agent
    Instance but never the human, so ownership is resolved server-side from durable
    instance state rather than accepted from a caller -- the same rule every owner-scoped
    write in this codebase already follows.
    """

    agent_instance_id: int
    run_id: int
    job_id: int
    attempt_id: int
    tool_sequence: int
    tool_definition_id: int
    upstream_name: str
    fingerprint: str
    arguments: Mapping[str, JsonValue]
    expires_in_seconds: int = DEFAULT_EXPIRY_SECONDS

    def __post_init__(self) -> None:
        if self.tool_sequence <= 0:
            raise InvalidApproval("tool sequence must be positive")
        if not MIN_EXPIRY_SECONDS <= self.expires_in_seconds <= MAX_EXPIRY_SECONDS:
            raise InvalidApproval("approval expiry is outside its bounds")


def input_digest(arguments: Mapping[str, JsonValue]) -> str:
    """SHA-256 of the canonical JSON of the exact action input."""
    return hashlib.sha256(canonical_json_text(dict(arguments)).encode("utf-8")).hexdigest()


def action_fingerprint(*, tool_definition_id: int, upstream_name: str, fingerprint: str) -> str:
    """Digest of the reviewed action contract: identity + reviewed definition fingerprint.

    Deliberately distinct from the definition fingerprint itself: the approval binds the
    *action* (this tool, under this reviewed definition) rather than the definition alone.
    """
    canonical = json.dumps(
        {
            "tool_definition_id": tool_definition_id,
            "upstream_name": upstream_name,
            "definition_fingerprint": fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def safe_preview(arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Build the owner-facing preview: bounded keys, and never raw text.

    **Every** string value is redacted to its byte count, not only long ones. A short string
    is exactly the shape a token, an email address, or a file path takes, and this preview is
    rendered in a browser and stored in a durable row; keeping "short enough to be harmless"
    text would make the disclosure depend on the argument rather than on the rule. Numbers and
    booleans are kept because they carry no free-form content.
    """
    preview: dict[str, JsonValue] = {}
    for key in sorted(arguments)[:16]:
        value = arguments[key]
        if isinstance(value, str):
            preview[key] = f"<string:{len(value.encode('utf-8'))} bytes>"
        elif isinstance(value, (bool, int, float)):
            preview[key] = value
        elif isinstance(value, Mapping):
            preview[key] = f"<object:{len(value)} keys>"
        elif isinstance(value, list):
            preview[key] = f"<array:{len(value)} items>"
        else:
            preview[key] = "<null>"
    return preview


class ApprovalService:
    """Owner decisions over the durable approval store."""

    def __init__(self, persistence: ApprovalPersistence, clock: Clock) -> None:
        self._persistence = persistence
        self._clock = clock

    def decide(
        self,
        *,
        owner_user_id: int,
        approval_id: int,
        approve: bool,
    ) -> ActionApproval:
        """Approve or deny one pending, unexpired, owned approval. Idempotent per state."""
        now = self._clock()
        if approve:
            return self._persistence.approve(
                owner_user_id=owner_user_id, approval_id=approval_id, now=now
            )
        return self._persistence.deny(owner_user_id=owner_user_id, approval_id=approval_id, now=now)

    def cancel(self, *, owner_user_id: int, approval_id: int) -> ActionApproval:
        return self._persistence.cancel(
            owner_user_id=owner_user_id, approval_id=approval_id, now=self._clock()
        )

    def get(self, *, owner_user_id: int, approval_id: int) -> ActionApproval:
        return self._persistence.get(owner_user_id=owner_user_id, approval_id=approval_id)

    def list_pending(self, *, owner_user_id: int) -> tuple[ActionApproval, ...]:
        return self._persistence.list_pending(owner_user_id=owner_user_id, now=self._clock())


class ApprovalPersistence(Protocol):
    """Durable approval operations. The consume gate is owned by the mediator path."""

    def request(self, command: ApprovalRequest, now: datetime) -> ActionApproval: ...

    def approve(self, *, owner_user_id: int, approval_id: int, now: datetime) -> ActionApproval: ...

    def deny(self, *, owner_user_id: int, approval_id: int, now: datetime) -> ActionApproval: ...

    def cancel(self, *, owner_user_id: int, approval_id: int, now: datetime) -> ActionApproval: ...

    def get(self, *, owner_user_id: int, approval_id: int) -> ActionApproval: ...

    def list_pending(
        self, *, owner_user_id: int, now: datetime | None = None
    ) -> tuple[ActionApproval, ...]: ...


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    """The typed result of the mediator's dispatch-time consumption gate.

    ``CONSUMED`` is the only branch that licenses dispatch. Every other branch fails closed
    with zero external calls.
    """

    kind: str  # consumed | unavailable


CONSUMED = "consumed"
CONSUMPTION_UNAVAILABLE = "unavailable"


class ApprovalAdmission(StrEnum):
    """What the gate decided about one action before its invocation row exists.

    ``NOT_REQUIRED`` is the whole Stage-D path for actions the policy does not cover.
    ``APPROVED`` is the only branch that may go on to consume and dispatch.
    """

    NOT_REQUIRED = "not_required"
    APPROVED = "approved"
    REQUESTED = "requested"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


def requires_approval(descriptor: ToolDescriptor) -> bool:
    """The frozen Stage-H rule for which actions a human must approve first.

    An external MCP action is the only thing in the current engine that leaves the machine on a
    model's behalf, so it is the only action class the gate covers. Built-in tools are
    unchanged Stage-D calls. The rule is code, not configuration: an operator cannot widen or
    narrow which action class is gated, exactly as the sandbox limits are not configuration.
    """
    return descriptor.source_kind is ToolSourceKind.MCP


class ApprovalGate(Protocol):
    """The mediator's approval port: one pre-dispatch check and one dispatch-time consume."""

    def admit(self, request: ApprovalRequest, *, now: datetime) -> ApprovalAdmission: ...

    def consume(
        self, request: ApprovalRequest, *, consuming_attempt_id: int, now: datetime
    ) -> ConsumeOutcome: ...

    def start(
        self, request: ApprovalRequest, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome: ...

    def abandon(self, request: ApprovalRequest, *, now: datetime) -> None: ...
