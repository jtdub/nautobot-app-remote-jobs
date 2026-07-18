"""Docker container runtime via aiodocker (SPEC 12.2, 12.3).

The agent needs the Docker (or Podman-compatible) socket mounted by the
operator. Job containers themselves NEVER get the socket — the hardening
defaults in :class:`~remote_worker.runtime.base.ContainerSpec` are applied
unconditionally here and the bind list is built only from configured
secret mounts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from typing import Any, AsyncIterator

try:  # imported lazily so unit tests run without the dependency installed
    import aiodocker
    from aiodocker.exceptions import DockerError
except ImportError:  # pragma: no cover - exercised only in minimal envs
    aiodocker = None  # type: ignore[assignment]

    class DockerError(Exception):  # type: ignore[no-redef]
        """Placeholder when aiodocker is unavailable."""

        status = 0


from .base import ContainerHandle, ContainerRuntime, ContainerSpec

logger = logging.getLogger(__name__)

_DOCKER_SOCKET_PATHS = ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock")


class DockerRuntime(ContainerRuntime):
    """Container runtime backed by the local Docker/Podman socket."""

    def __init__(self, docker_url: str | None = None) -> None:
        if aiodocker is None:
            raise RuntimeError(
                "the 'aiodocker' package is required for the Docker runtime; "
                "install the remote-worker package with its default dependencies"
            )
        self._docker_url = docker_url
        self._docker: Any = None

    def _client(self) -> Any:
        if self._docker is None:
            self._docker = aiodocker.Docker(url=self._docker_url)
        return self._docker

    @staticmethod
    def _encode_auth(auth: dict[str, str]) -> str:
        """Encode registry credentials as an X-Registry-Auth header value."""
        return base64.urlsafe_b64encode(json.dumps(auth).encode("utf-8")).decode("ascii")

    async def pull(self, image: str, auth: dict[str, str] | None = None) -> None:
        logger.info("pulling image %s", image)
        kwargs: dict[str, Any] = {}
        if auth:
            kwargs["auth"] = self._encode_auth(auth)
        await self._client().images.pull(image, **kwargs)

    async def create(self, spec: ContainerSpec) -> ContainerHandle:
        for mount in spec.mounts:
            if mount.source in _DOCKER_SOCKET_PATHS or mount.target in _DOCKER_SOCKET_PATHS:
                raise ValueError("refusing to mount a container engine socket into a job container")
        host_config: dict[str, Any] = {
            "ReadonlyRootfs": spec.read_only_rootfs,
            "NetworkMode": spec.network_mode,
            "AutoRemove": False,
            # Drop all Linux capabilities (defense in depth on top of the
            # non-root user + no-new-privileges): even if an operator overrides
            # the user back to root, retained caps like NET_RAW stay off.
            "CapDrop": ["ALL"],
        }
        if spec.tmpfs:
            host_config["Tmpfs"] = dict(spec.tmpfs)
        security_opt: list[str] = []
        if spec.no_new_privileges:
            security_opt.append("no-new-privileges:true")
        if security_opt:
            host_config["SecurityOpt"] = security_opt
        if spec.mounts:
            host_config["Binds"] = [f"{m.source}:{m.target}:{'ro' if m.read_only else 'rw'}" for m in spec.mounts]
        if spec.memory_bytes:
            host_config["Memory"] = spec.memory_bytes
        if spec.nano_cpus:
            host_config["NanoCpus"] = spec.nano_cpus
        config: dict[str, Any] = {
            "Image": spec.image,
            "Env": [f"{key}={value}" for key, value in spec.env.items()],
            "User": spec.user,
            "WorkingDir": spec.working_dir,
            "HostConfig": host_config,
        }
        logger.info("creating container %s from %s", spec.name, spec.image)
        container = await self._client().containers.create(config=config, name=spec.name)
        return DockerContainerHandle(container)

    async def get_by_name(self, name: str) -> ContainerHandle | None:
        try:
            container = await self._client().containers.get(name)
        except DockerError as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        return DockerContainerHandle(container)

    async def close(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None


class DockerContainerHandle(ContainerHandle):
    """Wraps one aiodocker container."""

    def __init__(self, container: Any) -> None:
        self._container = container
        self._exit_code: int | None = None
        self._wait_lock = asyncio.Lock()

    @property
    def id(self) -> str:
        """The container id."""
        return getattr(self._container, "id", "")

    async def start(self) -> None:
        await self._container.start()

    async def is_running(self) -> bool:
        """Whether the container is currently running."""
        info = await self._container.show()
        return bool(info.get("State", {}).get("Running", False))

    async def wait(self) -> int:
        if self._exit_code is None:
            async with self._wait_lock:
                if self._exit_code is None:
                    result = await self._container.wait()
                    self._exit_code = int(result.get("StatusCode", -1))
        return self._exit_code

    async def stream_logs(self) -> AsyncIterator[tuple[str, str]]:
        """Follow stdout and stderr concurrently, yielding tagged lines."""
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=1000)

        async def pump(output_type: str) -> None:
            try:
                async for line in self._container.log(
                    stdout=output_type == "stdout",
                    stderr=output_type == "stderr",
                    follow=True,
                ):
                    await queue.put((output_type, line))
            except (DockerError, OSError) as exc:
                logger.debug("log stream (%s) ended: %s", output_type, exc)

        pumps = [
            asyncio.create_task(pump("stdout"), name="log-pump-stdout"),
            asyncio.create_task(pump("stderr"), name="log-pump-stderr"),
        ]

        async def finalize() -> None:
            await asyncio.gather(*pumps, return_exceptions=True)
            await queue.put(None)

        finalizer = asyncio.create_task(finalize(), name="log-pump-finalizer")
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            for task in (*pumps, finalizer):
                task.cancel()
            await asyncio.gather(*pumps, finalizer, return_exceptions=True)

    async def _signal(self, signal: str) -> None:
        try:
            await self._container.kill(signal=signal)
        except DockerError as exc:
            # 409/404: already stopped or gone — treat as success.
            if getattr(exc, "status", None) in (404, 409, 500):
                logger.debug("signal %s ignored (container already stopped): %s", signal, exc)
                return
            raise

    async def terminate(self) -> None:
        await self._signal("SIGTERM")

    async def kill(self) -> None:
        await self._signal("SIGKILL")

    async def remove(self) -> None:
        with contextlib.suppress(DockerError):
            await self._container.delete(force=True)

    async def image_digest(self) -> str | None:
        try:
            info = await self._container.show()
        except DockerError:
            return None
        image = info.get("Image")
        if isinstance(image, str) and image.startswith("sha256:"):
            return image
        return None
