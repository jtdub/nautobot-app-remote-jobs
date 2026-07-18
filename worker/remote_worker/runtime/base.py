"""Container runtime abstraction (SPEC 12.2/12.3).

The agent talks to containers exclusively through these interfaces so a
Podman or Kubernetes runtime can be added without touching the agent
(K8s is phase 3).
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

#: Job images must always be referenced by digest (SPEC 4.1 / 12.2 step 1).
DIGEST_REFERENCE_RE = re.compile(r"@sha256:[0-9a-f]{64}$")

#: Default tmpfs mounted at /tmp inside job containers (read-only rootfs).
DEFAULT_TMPFS = {"/tmp": "rw,nosuid,nodev,size=67108864"}  # noqa: S108 - tmpfs inside the job container

#: Default non-root user for job containers ("nobody").
DEFAULT_USER = "65534:65534"


class RuntimeError_(RuntimeError):
    """Base class for runtime failures."""


class ImageReferenceError(ValueError):
    """Raised for image references that are not digest-pinned."""


def ensure_digest_reference(image: str) -> str:
    """Validate that *image* is pinned to a sha256 digest; return the digest.

    Raises:
        ImageReferenceError: when the reference lacks ``@sha256:<64 hex>``.
    """
    match = DIGEST_REFERENCE_RE.search(image)
    if not match:
        raise ImageReferenceError(f"refusing to run image without a sha256 digest reference: {image!r}")
    return match.group(0)[1:]  # strip the leading "@"


@dataclass(frozen=True)
class Mount:
    """A bind mount into a job container (secret mounts are read-only)."""

    source: str
    target: str
    read_only: bool = True


@dataclass
class ContainerSpec:
    """Everything needed to create one hardened job container.

    Hardening defaults per SPEC 12.3: read-only rootfs with tmpfs ``/tmp``,
    ``no-new-privileges``, default seccomp profile, non-root user, memory
    and CPU limits from agent config, and never a Docker socket mount.
    """

    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    memory_bytes: int | None = None
    nano_cpus: int | None = None
    read_only_rootfs: bool = True
    tmpfs: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TMPFS))
    no_new_privileges: bool = True
    user: str = DEFAULT_USER
    network_mode: str = "bridge"
    working_dir: str = "/tmp"  # noqa: S108 - container tmpfs workdir


class ContainerHandle(abc.ABC):
    """A created (possibly running) container."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the container."""

    @abc.abstractmethod
    async def wait(self) -> int:
        """Block until the container exits; return its exit code.

        Safe to call from multiple tasks; the exit code is cached.
        """

    @abc.abstractmethod
    def stream_logs(self) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(output_type, text)`` tuples, output_type in stdout/stderr."""

    @abc.abstractmethod
    async def terminate(self) -> None:
        """Send SIGTERM (graceful stop request). No-op if already stopped."""

    @abc.abstractmethod
    async def kill(self) -> None:
        """Send SIGKILL. No-op if already stopped."""

    @abc.abstractmethod
    async def remove(self) -> None:
        """Force-remove the container."""

    @abc.abstractmethod
    async def image_digest(self) -> str | None:
        """The digest of the image the container actually ran, if known."""


class ContainerRuntime(abc.ABC):
    """Factory/lifecycle interface for a container engine."""

    @abc.abstractmethod
    async def pull(self, image: str, auth: dict[str, str] | None = None) -> None:
        """Pull *image* (a digest-pinned reference)."""

    @abc.abstractmethod
    async def create(self, spec: ContainerSpec) -> ContainerHandle:
        """Create a container from *spec* (not started)."""

    @abc.abstractmethod
    async def get_by_name(self, name: str) -> ContainerHandle | None:
        """Look up an existing container by name (restart reconciliation)."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release engine connections."""
