"""Durable Tool Invocation state: its ports, its audit metadata, and its outcome vocabulary.

This module holds the application-layer contract for the one durable record Stage D keeps about a
tool call. Three things live here:

* the **ports** the tool loop uses to move an invocation through its lifecycle;
* the **audit metadata** contract -- argument digest/shape and the canonical result envelope;
* the **typed outcomes** every fenced write returns.

**Why outcomes are typed rather than boolean.** "Did the write happen?" is not a yes/no question
here. A refused `mark_started` could mean the grant was revoked, the definition drifted, the
connection was disabled, the owner no longer matched, the Run was cancelled, the lease expired, or
the Worker lost its claim -- facts with different durable meanings and different next actions.
Collapsing them into `False` would force every caller to guess, so each write returns the branch it
actually took.

**What this module is not.** It is not the executor, not the loop, not permission authority, and
not provider conversion. The authority that decides whether a call may run is D2's live predicate;
this module only records what that predicate decided.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast

from nervos_core.application.tool_permissions import PermissionDecision
from nervos_core.domain.tools import (
    JsonValue,
    ToolSourceKind,
    canonical_json_text,
)

# The durable `permission_decision` value for a call D2 allowed. The denied form is derived from the
# typed reason below, so the vocabulary has exactly one source.
ALLOWED_DECISION = "allowed"

# Argument-shape bounds. The durable column is bounded at 4096 bytes by a D1 CHECK, so these are the
# application-side statement of the same limit, plus the two the shape itself must respect.
MAX_ARGUMENT_SHAPE_KEYS = 32
MAX_ARGUMENT_SHAPE_KEY_LENGTH = 128
MAX_ARGUMENT_SHAPE_BYTES = 4096


class InvocationStatus(StrEnum):
    """The durable lifecycle of one tool call, as stored in `tool_invocations.status`."""

    REQUESTED = "requested"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"
    AMBIGUOUS = "ambiguous"


# The statuses that mean the call was dispatched. This is ADR 0017's own definition, restated here
# so the C4 replay guard and the loop cannot drift apart about what "dispatched" means.
DISPATCHED_STATUSES = frozenset(
    {
        InvocationStatus.STARTED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.AMBIGUOUS,
    }
)


class ClaimHandle(Protocol):
    """The authority every fenced tool-invocation write must present.

    Declared structurally rather than imported so this module depends on no orchestration module.
    `ClaimedAttempt` is the one implementation, and these seven values are exactly the ones every
    fenced Job write already checks.

    They are read-only properties because a claim handle *is* immutable authority: a frozen domain
    value with mutable-looking attributes could not satisfy this protocol, and accepting one that
    could be reassigned mid-Attempt would make the fence unprovable.
    """

    @property
    def job_id(self) -> int: ...

    @property
    def run_id(self) -> int: ...

    @property
    def attempt_id(self) -> int: ...

    @property
    def attempt_number(self) -> int: ...

    @property
    def worker_id(self) -> str: ...

    @property
    def claim_token(self) -> bytes: ...

    @property
    def lease_expires_at(self) -> datetime: ...


class RecordOutcomeKind(StrEnum):
    """What `record_requested` durably did."""

    REQUESTED = "requested"
    # Execution authority was gone: no row was inserted. The loop stops without dispatching.
    FENCED = "fenced"


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    kind: RecordOutcomeKind
    invocation_id: int | None = None


class StartOutcomeKind(StrEnum):
    """The one branch `mark_started` took."""

    STARTED = "started"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FENCED = "fenced"


@dataclass(frozen=True, slots=True)
class StartOutcome:
    """The typed result of re-checking authority immediately before dispatch.

    `permission_decision` is the rendered durable value when the branch was a denial, carried for
    audit. The loop branches only on `kind`; the string never authorizes anything.
    """

    kind: StartOutcomeKind
    invocation_id: int | None = None
    permission_decision: str | None = None


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    """The content evidence for one concluded call: its digest and its true byte size.

    Neither value is an identity, an idempotency key, or a dedup key -- a digest is evidence that
    two results are the same bytes, and nothing more. Both are computed from the *untruncated*
    canonical envelope, so a truncated model-visible observation still leaves the full evidence.
    """

    digest: str
    bytes: int


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """Everything one `requested` row must carry, before it has a durable id."""

    run_id: int
    job_id: int
    attempt_id: int
    tool_sequence: int
    tool_definition_id: int
    source_kind: ToolSourceKind
    source_id: int | None
    upstream_name: str
    model_name: str
    definition_fingerprint: str
    provider_call_id: str
    permission_decision: PermissionDecision
    arguments: Mapping[str, JsonValue]


class ToolInvocationPersistence(Protocol):
    """Durable tool-invocation writes. Every operation is fenced and short.

    No operation spans an await: the loop performs one bounded `BEGIN IMMEDIATE` transaction per
    transition, and never holds a database session across a model call, a tool call, or a retry.
    """

    def record_requested(
        self, *, claim: ClaimHandle, request: InvocationRequest, now: datetime
    ) -> RecordOutcome: ...

    def mark_started(
        self, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome: ...

    def mark_denied(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        decision: PermissionDecision,
        now: datetime,
    ) -> bool: ...

    def mark_cancelled(self, *, claim: ClaimHandle, invocation_id: int, now: datetime) -> bool: ...

    def mark_succeeded(
        self, *, claim: ClaimHandle, invocation_id: int, envelope: ResultEnvelope, now: datetime
    ) -> bool: ...

    def mark_failed(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool: ...

    def mark_ambiguous(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool: ...


def permission_decision_value(decision: PermissionDecision) -> str:
    """Render a typed D2 decision as its durable audit string.

    `PermissionDenialReason` is an `Enum` built from `auto()`, so its *name* is the stable
    vocabulary -- `str(member)` would render `PermissionDenialReason.NOT_GRANTED`, which neither
    matches the D1 `denied_*` shape nor stays stable if the members are reordered. D2 states that
    its reasons never become string-based security logic; they do not here either. The string
    records what a live predicate decided, and is never the input to a decision.
    """
    if decision.allowed:
        return ALLOWED_DECISION
    if decision.reason is None:  # pragma: no cover - PermissionDecision forbids this shape
        raise ValueError("denied decision must have a reason")
    return f"denied_{decision.reason.name.lower()}"


def canonical_arguments(arguments: Mapping[str, JsonValue]) -> str:
    """Return the canonical JSON text of one validated argument object."""
    return canonical_json_text(cast("JsonValue", dict(arguments)))


def arguments_digest(arguments: Mapping[str, JsonValue]) -> str:
    """SHA-256 over the canonical text of the full validated argument object."""
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def arguments_shape(arguments: Mapping[str, JsonValue]) -> str | None:
    """Return a value-free skeleton of the argument object's top-level keys, or ``None``.

    The shape is the sorted, exact key names -- no values, no types, nothing a tool call would have
    carried. It is deliberately all-or-nothing: if any key is too long, there are too many keys, or
    the encoded skeleton would exceed its bound, the result is ``None`` rather than a truncated or
    renamed one. A shortened key could make two distinct calls look identical in durable audit
    metadata, which is exactly the lie this metadata exists to prevent.
    """
    keys = sorted(arguments)
    if len(keys) > MAX_ARGUMENT_SHAPE_KEYS:
        return None
    if any(len(key) > MAX_ARGUMENT_SHAPE_KEY_LENGTH for key in keys):
        return None
    encoded = canonical_json_text(cast("JsonValue", list(keys)))
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_SHAPE_BYTES:
        return None
    return encoded


def canonical_envelope_bytes(*, text: str | None, structured: JsonValue | None) -> bytes:
    """Return the canonical UTF-8 bytes of one tool result envelope.

    The envelope is one object with two named members, so there is exactly one serialization for a
    given result. `structured=None` and `structured={}` are therefore different envelopes, as they
    are different facts.
    """
    envelope: dict[str, JsonValue] = {"structured": structured, "text": text}
    return canonical_json_text(envelope).encode("utf-8")


def result_envelope(*, text: str | None, structured: JsonValue | None) -> ResultEnvelope:
    """Compute the digest and byte count from one canonical envelope, in one call.

    Both values come from the same bytes, so they cannot disagree about what the result was.
    """
    payload = canonical_envelope_bytes(text=text, structured=structured)
    return ResultEnvelope(hashlib.sha256(payload).hexdigest(), len(payload))


def parse_arguments(arguments_json: str) -> dict[str, JsonValue] | None:
    """Parse a canonical argument payload into an object, or return ``None``.

    A provider may legally emit any JSON here, so this is a classification rather than a
    guarantee: a non-object, malformed, or non-JSON value is `None`, and the caller turns that into
    an arguments-invalid outcome. Deeper structure is the tool's own canonical schema's business,
    not this parse's.
    """
    try:
        parsed: object = json.loads(arguments_json)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, JsonValue]", parsed)
