"""Safe public error handling for the NervOS API."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from nervos_core.application.account_connections import (
    ConnectionNotFound,
    InvalidConnection,
)
from nervos_core.application.account_oauth import AccountAuthorizationUnavailable
from nervos_core.application.agent_definitions import UnknownAgentDefinition
from nervos_core.application.agents import (
    AgentInstanceNotFound,
    AgentInstanceUnavailable,
    RunNotFound,
)
from nervos_core.application.approvals import (
    ApprovalConflict,
    ApprovalNotFound,
    InvalidApproval,
)
from nervos_core.application.authentication import (
    AuthenticationRequired,
    InvalidCredentials,
    InvalidPassword,
    InvalidUsername,
    PasswordWorkLimit,
    PersistenceUnavailable,
    SetupComplete,
)
from nervos_core.application.conversations import (
    ConversationArchived,
    ConversationBusy,
    ConversationConflict,
    ConversationDeleted,
    ConversationNotFound,
    TurnNotFound,
    TurnNotRetryable,
)
from nervos_core.application.errors import (
    PersistenceUnavailable as ApplicationPersistenceUnavailable,
)
from nervos_core.application.errors import QueueCapacityExceeded
from nervos_core.application.mcp_connections import InvalidMcpConnection
from nervos_core.application.model_providers import (
    ModelProviderUnavailable,
    UnknownModelProvider,
)
from nervos_core.application.package_config_schema import (
    PackageConfigSchemaError,
    PackageConfigValidationError,
)
from nervos_core.application.package_installation import (
    PackageAuthorizationMismatch,
    PackageHealthCheckFailed,
)
from nervos_core.application.publisher_trust import InvalidTrustState, PublisherRevoked
from nervos_core.application.run_cancellation import RunNotCancellable
from nervos_core.application.runtime_integration import IntegrationConflict, IntegrationNotFound
from nervos_core.application.sandbox import ContainmentUnavailable
from nervos_core.application.secrets import (
    SecretAlreadyExists,
    SecretNotFound,
    SecretReferenced,
    SecretStoreUnavailable,
)
from nervos_core.application.tool_permissions import McpConnectionNotFound
from nervos_core.application.triggers import (
    TriggerConfigConflict,
    TriggerHasHistory,
    TriggerNotEditable,
    TriggerNotFound,
)
from nervos_core.application.trusted_chat import UnknownAgentHandler
from nervos_core.application.workflows import WorkflowCapacityExceeded
from nervos_core.domain.agents import InvalidAgentDefinitionId, InvalidAgentInstance
from nervos_core.domain.conversations import InvalidConversation
from nervos_core.domain.memory import (
    InvalidMemory,
    InvalidMemorySource,
    MemoryNotFound,
    MemoryScopeCapacityExceeded,
    StaleMemoryVersion,
)
from nervos_core.domain.package_query import (
    ConfigCarryForwardIncompatible,
    ImmutableConfigViolation,
    IncompatiblePackageVersion,
    PackageHasActiveRuns,
    PackageHasBoundInstances,
    StaleConfigRevision,
)
from nervos_core.domain.runs import InvalidRun
from nervos_core.domain.scheduling import ScheduleCalculationError
from nervos_core.domain.secrets import InvalidSecret
from nervos_core.domain.triggers import InvalidTrigger
from nervos_core.domain.workflows import (
    InvalidWorkflow,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowTransitionError,
)
from nervos_core.infrastructure.database.packages import (
    InstalledPackageNotFound,
    PackageIdentityConflict,
    PackageInstallInProgress,
)

from nervos_api.api.schemas import ErrorDetail, ErrorResponse
from nervos_api.application.marketplace_installation import (
    MarketplaceRequestConflict,
    MarketplaceRequestNotFound,
)


class InvalidOrigin(Exception):
    """Raised when an unsafe request lacks the exact configured Origin."""


class McpConnectionDeleteRefused(Exception):
    """Raised when a connection cannot be hard-deleted without destroying evidence.

    The two refusals are separate types rather than one carrying a reason, because the error map
    is keyed by exact type: a caller cannot then accidentally widen one into the other, and the
    response body stays a fixed NervOS-authored sentence.
    """


class McpConnectionHasHistory(McpConnectionDeleteRefused):
    """A tool invocation from this connection exists, so deleting it would erase audit truth."""


class McpConnectionHasLiveRun(McpConnectionDeleteRefused):
    """A nonterminal Run's grants could still reach this connection's tools."""


class UnsupportedB3AgentDefinition(Exception):
    """Raised when a structurally valid definition identity is outside this milestone's surface.

    The resolver still decides whether an exact pair exists. This is a narrower NervOS-owned
    surface restriction that keeps later-registered definitions from becoming creatable here
    before their own milestone authorizes it.
    """


_ERROR_MAP: dict[type[Exception], tuple[int, str, str]] = {
    InvalidUsername: (422, "invalid_username", "Username does not meet the required format."),
    InvalidPassword: (422, "invalid_password", "Password does not meet the required length."),
    SetupComplete: (409, "setup_complete", "Initial setup is already complete."),
    InvalidCredentials: (401, "invalid_credentials", "Invalid username or password."),
    AuthenticationRequired: (401, "authentication_required", "Authentication is required."),
    PersistenceUnavailable: (
        503,
        "service_unavailable",
        "Authentication is temporarily unavailable.",
    ),
    InvalidOrigin: (403, "invalid_origin", "Request origin is not allowed."),
    PasswordWorkLimit: (429, "too_many_attempts", "Too many authentication attempts."),
}

# Agent/Run domain failures. Dispatched by exact type through `agent_error_handler`, which is
# registered for every key here. Static NervOS-owned messages only: no provider text, body,
# header, credential, or stack trace is ever propagated.
AGENT_ERROR_MAP: dict[type[Exception], tuple[int, str, str]] = {
    AccountAuthorizationUnavailable: (
        409,
        "account_authorization_unavailable",
        "Account authorization could not be completed.",
    ),
    # Stage H. The secret store is fail-closed: an unusable master key is a 503, never a
    # silent plaintext fallback, and no message below ever names a secret, a key, or a
    # credential.
    SecretStoreUnavailable: (
        503,
        "secret_store_unavailable",
        "The secret store is unavailable; no secret operation was performed.",
    ),
    SecretNotFound: (404, "secret_not_found", "The secret was not found."),
    InvalidSecret: (422, "invalid_secret", "The secret request is invalid."),
    SecretAlreadyExists: (409, "secret_already_exists", "A secret with that name already exists."),
    SecretReferenced: (
        409,
        "secret_referenced",
        "An account connection still references this secret.",
    ),
    ConnectionNotFound: (
        404,
        "account_connection_not_found",
        "The account connection was not found.",
    ),
    InvalidConnection: (
        422,
        "invalid_account_connection",
        "The account connection configuration is invalid.",
    ),
    ApprovalNotFound: (404, "action_approval_not_found", "The action approval was not found."),
    ApprovalConflict: (
        409,
        "action_approval_conflict",
        "The action approval is no longer awaiting that decision.",
    ),
    InvalidApproval: (422, "invalid_action_approval", "The action approval request is invalid."),
    PublisherRevoked: (
        409,
        "publisher_revoked",
        "This package's publisher has been revoked locally.",
    ),
    InvalidTrustState: (422, "invalid_trust_state", "The publisher trust state is invalid."),
    IntegrationNotFound: (404, "integration_not_found", "The requested resource was not found."),
    IntegrationConflict: (
        409,
        "integration_conflict",
        "The configuration changed or does not match; refresh before continuing.",
    ),
    MarketplaceRequestNotFound: (
        404,
        "marketplace_request_not_found",
        "Install request not found.",
    ),
    MarketplaceRequestConflict: (
        409,
        "marketplace_request_conflict",
        "The exact release cannot be installed from this request.",
    ),
    InvalidAgentDefinitionId: (
        422,
        "invalid_agent_definition",
        "The agent definition identity is invalid.",
    ),
    UnsupportedB3AgentDefinition: (
        422,
        "unsupported_agent_definition",
        "This NervOS milestone only supports the trusted nervos.chat versions 1 and 2.",
    ),
    UnknownAgentDefinition: (
        422,
        "unknown_agent_definition",
        "The requested agent definition version is not available.",
    ),
    UnknownModelProvider: (422, "unknown_model_provider", "The model provider is not supported."),
    InvalidAgentInstance: (
        422,
        "invalid_agent_instance",
        "The agent instance configuration is invalid.",
    ),
    InvalidRun: (422, "invalid_input", "The submitted input is invalid."),
    InvalidMcpConnection: (
        422,
        "invalid_mcp_connection",
        "The MCP connection configuration is invalid.",
    ),
    McpConnectionNotFound: (404, "mcp_connection_not_found", "The MCP connection was not found."),
    McpConnectionHasHistory: (
        409,
        "mcp_connection_has_history",
        "This MCP connection has recorded tool invocations and cannot be deleted.",
    ),
    McpConnectionHasLiveRun: (
        409,
        "mcp_connection_has_live_run",
        "An active run can still reach this MCP connection's tools, so it cannot be deleted.",
    ),
    AgentInstanceNotFound: (404, "agent_instance_not_found", "The agent instance was not found."),
    RunNotFound: (404, "run_not_found", "The run was not found."),
    RunNotCancellable: (
        409,
        "run_not_cancellable",
        "The run already reached a terminal state and cannot be cancelled.",
    ),
    AgentInstanceUnavailable: (
        409,
        "agent_instance_unavailable",
        "The agent instance is not eligible to create new runs.",
    ),
    ModelProviderUnavailable: (
        409,
        "model_provider_unavailable",
        "The model provider is not configured for this NervOS process.",
    ),
    UnknownAgentHandler: (
        503,
        "service_unavailable",
        "The trusted agent behavior is unavailable.",
    ),
    QueueCapacityExceeded: (
        429,
        "queue_capacity_exceeded",
        "NervOS cannot accept more runs until its pending queue drains.",
    ),
    ApplicationPersistenceUnavailable: (
        503,
        "service_unavailable",
        "NervOS storage is temporarily unavailable.",
    ),
    TriggerNotFound: (404, "trigger_not_found", "The trigger was not found."),
    TriggerHasHistory: (
        409,
        "trigger_has_history",
        "This trigger has occurrences and cannot be deleted.",
    ),
    TriggerNotEditable: (
        409,
        "trigger_not_editable",
        "This change is not permitted for the trigger's current state.",
    ),
    TriggerConfigConflict: (
        409,
        "stale_trigger",
        "The trigger changed since it was read; re-read it and retry.",
    ),
    InvalidTrigger: (422, "invalid_trigger", "The trigger configuration is invalid."),
    ScheduleCalculationError: (
        422,
        "invalid_schedule",
        "The schedule could not be evaluated.",
    ),
    ConversationNotFound: (
        404,
        "conversation_not_found",
        "The conversation was not found.",
    ),
    ConversationBusy: (
        409,
        "conversation_busy",
        "The conversation has an active turn; wait for it to complete.",
    ),
    ConversationConflict: (
        409,
        "conversation_conflict",
        "The client message ID was already used with different content.",
    ),
    TurnNotFound: (
        404,
        "turn_not_found",
        "The turn was not found.",
    ),
    TurnNotRetryable: (
        409,
        "turn_not_retryable",
        "The turn is not eligible for retry.",
    ),
    InvalidConversation: (
        422,
        "invalid_conversation",
        "The conversation input is invalid.",
    ),
    InvalidMemory: (
        422,
        "invalid_memory",
        "The memory input is invalid.",
    ),
    InvalidMemorySource: (
        422,
        "invalid_memory_source",
        "The memory promotion source is invalid or not eligible.",
    ),
    MemoryNotFound: (
        404,
        "memory_not_found",
        "The memory was not found.",
    ),
    MemoryScopeCapacityExceeded: (
        409,
        "memory_scope_capacity_exceeded",
        "The memory scope has reached its maximum capacity.",
    ),
    StaleMemoryVersion: (
        409,
        "stale_memory_version",
        "The memory version is stale.",
    ),
    ConversationArchived: (
        409,
        "conversation_archived",
        "The conversation is archived.",
    ),
    ConversationDeleted: (
        404,
        "conversation_not_found",
        "The conversation was not found.",
    ),
    PackageAuthorizationMismatch: (
        400,
        "package_authorization_mismatch",
        "The approved package metadata does not match the verified archive.",
    ),
    IncompatiblePackageVersion: (
        400,
        "incompatible_nervos_version",
        "The package is not compatible with this version of NervOS.",
    ),
    InstalledPackageNotFound: (
        404,
        "package_version_not_found",
        "The requested package version was not found.",
    ),
    PackageIdentityConflict: (
        409,
        "package_identity_conflict",
        "A package with this identifier and version already exists with different contents.",
    ),
    PackageInstallInProgress: (
        409,
        "package_install_in_progress",
        "An installation for this package version is currently in progress.",
    ),
    PackageHasBoundInstances: (
        409,
        "package_has_bound_instances",
        "The package version cannot be removed because agent instances are bound to it.",
    ),
    PackageHasActiveRuns: (
        409,
        "package_has_active_runs",
        "The package version has active runs and cannot be immediately removed.",
    ),
    StaleConfigRevision: (
        409,
        "stale_config_revision",
        "The agent instance configuration was modified concurrently. Please refresh.",
    ),
    ImmutableConfigViolation: (
        422,
        "immutable_configuration_modified",
        "An immutable configuration field cannot be modified.",
    ),
    ConfigCarryForwardIncompatible: (
        422,
        "config_carry_forward_incompatible",
        "The current configuration is incompatible with the target package version.",
    ),
    PackageConfigValidationError: (
        422,
        "package_config_validation_error",
        "The provided configuration is invalid for this package schema.",
    ),
    PackageConfigSchemaError: (
        422,
        "package_config_schema_error",
        "The package configuration schema is invalid.",
    ),
    PackageHealthCheckFailed: (
        422,
        "package_health_check_failed",
        "The package activation health check failed.",
    ),
    ContainmentUnavailable: (
        503,
        ContainmentUnavailable.CODE,
        ContainmentUnavailable.MESSAGE,
    ),
    # W5b durable workflows. `WorkflowNotFound` covers both "no such workflow" and "belongs to
    # another owner", and returns one identical answer for both, so the response cannot be used
    # to discover another owner's workflow id. `WorkflowTransitionError` is a subclass of it and
    # is registered separately: the workflow *does* exist and is owned, so only the requested
    # transition is illegal, and telling those two apart leaks nothing an owner did not already
    # know about their own workflow.
    WorkflowNotFound: (404, "workflow_not_found", "The workflow was not found."),
    WorkflowTransitionError: (
        409,
        "workflow_transition_conflict",
        "The workflow is no longer in a state that allows this action.",
    ),
    WorkflowConflict: (
        409,
        "workflow_conflict",
        "The workflow changed or does not match; refresh before continuing.",
    ),
    WorkflowCapacityExceeded: (
        409,
        "workflow_capacity_exceeded",
        "Too many workflows are already active for this owner.",
    ),
    InvalidWorkflow: (422, "invalid_workflow", "The workflow request is invalid."),
}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build a stable non-cacheable public error response."""
    content = ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump()
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Return a generic production-safe response for unexpected failures."""
    del request, error
    return error_response(500, "internal_server_error", "An unexpected error occurred.")


async def authentication_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Map expected authentication failures without exposing internals."""
    del request
    status_code, code, message = _ERROR_MAP[type(error)]
    return error_response(status_code, code, message)


async def agent_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Map expected Agent Instance and Run failures without exposing internals."""
    del request
    status_code, code, message = AGENT_ERROR_MAP[type(error)]
    return error_response(status_code, code, message)


async def validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """Return sanitized validation details without rejected input values."""
    del request
    allowed_fields = {"username", "password"}
    fields = sorted(
        {
            str(part)
            for item in error.errors()
            for part in item["loc"]
            if isinstance(part, str) and part in allowed_fields
        }
    )
    message = "Request validation failed."
    if fields:
        message = f"Request validation failed for: {', '.join(fields)}."
    return error_response(422, "validation_error", message)
