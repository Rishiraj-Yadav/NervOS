"""Process-incarnation identity for one Worker.

A Worker identity is per process, not per row: C2 ships no Workers registry, so nothing
durable refers to it. It is opaque, not a secret, never user-facing, and never written into a
Run Event.
"""

from __future__ import annotations

import os
import socket
from uuid import uuid4

MAX_WORKER_ID_LENGTH = 128


def generate_worker_id() -> str:
    """Return a bounded identifier that is unique across local processes and restarts."""
    hostname = socket.gethostname()[:32]
    identifier = f"{hostname}-{os.getpid()}-{uuid4().hex[:16]}"
    return identifier[:MAX_WORKER_ID_LENGTH]
