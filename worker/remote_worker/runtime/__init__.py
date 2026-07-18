"""Container runtime abstraction and implementations.

v1 ships Docker/Podman-socket support via aiodocker; a Kubernetes runtime
is planned for phase 3 (SPEC 12.1).
"""

from .base import (
    ContainerHandle,
    ContainerRuntime,
    ContainerSpec,
    ImageReferenceError,
    Mount,
    ensure_digest_reference,
)

__all__ = [
    "ContainerHandle",
    "ContainerRuntime",
    "ContainerSpec",
    "ImageReferenceError",
    "Mount",
    "ensure_digest_reference",
]
