"""Internal package-host runtime; not part of the public Agent SDK."""

from nervos_package_host.wire import (
    GENERIC_PROCESS_BRIDGE_CAPABILITY,
    HOST_CAPABILITIES,
    HOST_PROTOCOL_VERSION,
    WORKFLOW_HOST_CAPABILITY,
)

__version__ = "0.1.0"

__all__ = [
    "GENERIC_PROCESS_BRIDGE_CAPABILITY",
    "HOST_CAPABILITIES",
    "HOST_PROTOCOL_VERSION",
    "WORKFLOW_HOST_CAPABILITY",
    "__version__",
]
