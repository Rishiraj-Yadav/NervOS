"""Concrete model-provider adapters for NervOS.

Only this package may import a provider SDK. The provider-neutral port lives in
`nervos_core.application.model_completion`.
"""

from nervos_models.anthropic import (
    PROVIDER_ID,
    AnthropicModelCompletion,
    create_anthropic_client,
)
from nervos_models.composition import (
    ModelProviderComposition,
    close_model_providers,
    compose_model_providers,
)

__all__ = [
    "PROVIDER_ID",
    "AnthropicModelCompletion",
    "ModelProviderComposition",
    "close_model_providers",
    "compose_model_providers",
    "create_anthropic_client",
]
